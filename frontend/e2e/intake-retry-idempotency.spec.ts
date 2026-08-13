import { expect, test } from "@playwright/test";
import { fictionalApplicant, submitApplication } from "./fixtures";

/**
 * The borrower flow must not create a second applicant when intake fails.
 *
 * The backend has supported an idempotency key and a resume token since
 * db/migrations/0036 and 0037, and the browser never sent one -- so a real
 * borrower retrying after a KYC failure still produced two applicants and two
 * applications. The contract only exists if the client participates in it.
 *
 * This drives the real apply flow, not the API, because the defect was in the
 * client: the key has to be minted before the first submission, survive the
 * failure, and be sent again on the retry.
 */
test("a retry after an intake failure reuses the same idempotency key", async ({ page }) => {
  const applicant = fictionalApplicant("Retry", /* even ssn */ true, 100_000);

  // Fail the first submission at the gateway, exactly as a KYC outage would.
  let attempts = 0;
  const keysSeen: string[] = [];
  const secretsSeen: (string | undefined)[] = [];
  await page.route("**/los/applications", async (route) => {
    const body = route.request().postDataJSON() as { idempotency_key?: string };
    keysSeen.push(body?.idempotency_key ?? "");
    secretsSeen.push(route.request().headers()["x-resume-token"]);
    attempts += 1;
    if (attempts === 1) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({
          detail: {
            error: "identity_verification_unavailable",
            message: "This application was recorded but not verified.",
            app_id: 4242,
            access_token: "acc-tok",
            // A server-minted token. The client must NOT adopt it -- see the
            // assertion below. It is here because a real deployment mid-rollout
            // may still send one, and adopting it would rebuild the defect.
            resume_token: "server-minted-do-not-adopt",
            resume: "POST /applications with the same idempotency_key",
          },
        }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        app_id: 4242, status: "submitted",
        resume_token: "server-minted-do-not-adopt-2",
        access_token: "acc-tok-2",
        kyc: { name_verified: true, dob_verified: true, address_verified: true, ssn_verified: true },
      }),
    });
  });

  await submitApplication(page, applicant, { stopAtReview: true });
  await page.getByRole("button", { name: /submit application/i }).click();
  // The alert element rather than its wording: what this test is about is the
  // KEY on the retry, and pinning the copy would make it fail on a reword.
  await expect(page.locator(".alert-error").first()).toBeVisible();

  // Retry the same draft.
  await page.getByRole("button", { name: /submit application/i }).click();
  await expect.poll(() => attempts).toBeGreaterThanOrEqual(2);

  expect(keysSeen).toHaveLength(2);
  expect(keysSeen[0]).toBeTruthy();
  expect(keysSeen[1]).toBe(keysSeen[0]);

  // The ordering that makes a lost response survivable: the credential is on
  // the FIRST request, before any response exists. A client that waits to be
  // issued one has nothing to retry with when the response never arrives.
  expect(secretsSeen[0]).toBeTruthy();
  expect(secretsSeen[1]).toBe(secretsSeen[0]);
  expect(secretsSeen[1]).not.toBe("server-minted-do-not-adopt");
});

test("a lost first response does not strand the applicant", async ({ page }) => {
  // The reported failure, driven through the real UI: the first submission is
  // received by the server and its RESPONSE never comes back. The browser
  // learns nothing from it -- so whatever it needs to retry, it must already
  // have had.
  const applicant = fictionalApplicant("Lost", true, 100_000);

  let attempts = 0;
  const secretsSeen: (string | undefined)[] = [];
  await page.route("**/los/applications", async (route) => {
    attempts += 1;
    secretsSeen.push(route.request().headers()["x-resume-token"]);
    if (attempts === 1) {
      // Not an error response -- no response at all, which is the case a
      // status-code test cannot reach.
      await route.abort("connectionaborted");
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        app_id: 4242, status: "submitted", access_token: "acc-tok",
        kyc: { name_verified: true, dob_verified: true, address_verified: true, ssn_verified: true },
      }),
    });
  });

  await submitApplication(page, applicant, { stopAtReview: true });
  await page.getByRole("button", { name: /submit application/i }).click();
  await expect(page.locator(".alert-error").first()).toBeVisible();

  await page.getByRole("button", { name: /submit application/i }).click();
  await expect.poll(() => attempts).toBeGreaterThanOrEqual(2);

  expect(secretsSeen[0]).toBeTruthy();
  expect(secretsSeen[1]).toBe(secretsSeen[0]);
});

test("the idempotency key is never put in a URL", async ({ page }) => {
  const applicant = fictionalApplicant("NoLeak", true, 100_000);
  const urls: string[] = [];
  page.on("request", (r) => urls.push(r.url()));

  await submitApplication(page, applicant, { stopAtReview: true });

  for (const url of urls) {
    expect(url).not.toMatch(/idempotency_key|resume_token/i);
  }
});

test("a second application in the same tab does not reuse the first one's credentials", async ({ page }) => {
  // The lifecycle defect, driven through the real UI.
  //
  // The retry credentials used to be cleared only at OFFER ACCEPTANCE. Most
  // applications never get there -- denied, referred, or simply abandoned -- so
  // the key and secret stayed live in the tab. The next application submitted
  // in that tab reused them, the server took the matching pair as proof of
  // ownership, returned the FIRST application, and dropped the second person's
  // data entirely.
  //
  // Reproduced against a running stack before the fix: a second submission with
  // a different name, DOB, SSN, address and amount came back with the first
  // applicant's app_id and a live access token for it, and no second applicant
  // row was created at all. On a shared or kiosk browser that is one person
  // being handed another person's application.
  const first = fictionalApplicant("DeniedFirst", /* odd ssn -> denied */ false, 30_000);
  const second = fictionalApplicant("SecondPerson", true, 140_000);

  const keysSeen: string[] = [];
  const secretsSeen: (string | undefined)[] = [];
  let attempts = 0;

  await page.route("**/los/applications", async (route) => {
    const body = route.request().postDataJSON() as { idempotency_key?: string };
    keysSeen.push(body?.idempotency_key ?? "");
    secretsSeen.push(route.request().headers()["x-resume-token"]);
    attempts += 1;
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        // Distinct ids: if the second submission somehow resumed the first,
        // the assertion below on a FRESH key is what catches it, and this
        // makes the intent readable.
        app_id: attempts === 1 ? 4242 : 5353,
        status: "submitted",
        access_token: `acc-tok-${attempts}`,
        kyc: { name_verified: true, dob_verified: true, address_verified: true, ssn_verified: true },
      }),
    });
  });

  // First application: submitted, then DENIED. It never reaches offer
  // acceptance, which is precisely the path that used to leak.
  await submitApplication(page, first, { stopAtReview: true });
  await page.getByRole("button", { name: /submit application/i }).click();
  await expect(page.getByText("Step 5 of 5")).toBeVisible({ timeout: 15_000 });

  // The applicant closes the decision and starts over in the same tab.
  await page.reload();

  await submitApplication(page, second, { stopAtReview: true });
  await page.getByRole("button", { name: /submit application/i }).click();
  await expect.poll(() => attempts).toBeGreaterThanOrEqual(2);

  expect(keysSeen).toHaveLength(2);
  expect(keysSeen[0]).toBeTruthy();
  expect(keysSeen[1]).toBeTruthy();
  expect(keysSeen[1]).not.toBe(keysSeen[0]);

  // The recovery secret must rotate with it. A fresh key paired with the old
  // secret would still be wrong -- and would read as fixed.
  expect(secretsSeen[1]).toBeTruthy();
  expect(secretsSeen[1]).not.toBe(secretsSeen[0]);
});

test("the credentials are gone from storage the moment intake succeeds", async ({ page }) => {
  // The state assertion behind the test above. That one proves the NEXT
  // submission differs; this proves WHY, by reading the tab's storage at the
  // instant intake returns -- before any reload, decision, or acceptance.
  const applicant = fictionalApplicant("ClearOnSuccess", true, 100_000);

  // Captured from the request itself. The credentials are minted lazily, at
  // submit time -- so there is no moment when the form is merely open and they
  // are already in storage. Reading them off the wire is what proves they
  // existed, which is what stops the storage assertion below being vacuous: an
  // empty sessionStorage in a tab that never had a key would otherwise "pass".
  let sentKey: string | undefined;
  let sentSecret: string | undefined;

  await page.route("**/los/applications", async (route) => {
    const body = route.request().postDataJSON() as { idempotency_key?: string };
    sentKey = body?.idempotency_key;
    sentSecret = route.request().headers()["x-resume-token"];
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        app_id: 4242, status: "submitted", access_token: "acc-tok",
        kyc: { name_verified: true, dob_verified: true, address_verified: true, ssn_verified: true },
      }),
    });
  });

  await submitApplication(page, applicant, { stopAtReview: true });
  await page.getByRole("button", { name: /submit application/i }).click();
  await expect(page.getByText("Step 5 of 5")).toBeVisible({ timeout: 15_000 });

  // They really were minted and really were sent...
  expect(sentKey).toBeTruthy();
  expect(sentSecret).toBeTruthy();

  // ...and they are gone the instant intake returned -- before any reload,
  // decision or offer acceptance.
  const after = await page.evaluate(() => ({
    key: sessionStorage.getItem("meridian.intake.idempotency_key"),
    secret: sessionStorage.getItem("meridian.intake.resume_token"),
  }));
  expect(after.key).toBeNull();
  expect(after.secret).toBeNull();
});

test("a corrected resubmission after a payload-mismatch 409 is not a dead end", async ({ page }) => {
  // The escape hatch for the 409 this PR introduced.
  //
  // The server refuses a retry whose applicant or loan details changed, and
  // tells the borrower to start a new application. The client kept the same
  // key and secret for every error, so the next submit in the same tab
  // presented them again, hit the same stored application, and got the same
  // 409. sessionStorage survives a reload, so the only way out was closing the
  // tab -- which nothing on screen said. The instruction was not actionable.
  const applicant = fictionalApplicant("Mismatch", true, 100_000);

  const keysSeen: string[] = [];
  let attempts = 0;

  await page.route("**/los/applications", async (route) => {
    const body = route.request().postDataJSON() as { idempotency_key?: string };
    keysSeen.push(body?.idempotency_key ?? "");
    attempts += 1;
    if (attempts === 1) {
      // The borrower corrected a detail; the server refuses the retry.
      await route.fulfill({
        status: 409,
        contentType: "application/json",
        body: JSON.stringify({
          detail: {
            error: "retry_payload_changed",
            message:
              "This retry carries different applicant or loan details from the "
              + "application it is retrying. Start a new application to submit "
              + "changed information.",
          },
        }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        app_id: 7777, status: "submitted", access_token: "acc-tok",
        kyc: { name_verified: true, dob_verified: true, address_verified: true, ssn_verified: true },
      }),
    });
  });

  await submitApplication(page, applicant, { stopAtReview: true });
  await page.getByRole("button", { name: /submit application/i }).click();
  await expect(page.locator(".alert-error").first()).toBeVisible();

  // The borrower does what the message told them to: submit again.
  await page.getByRole("button", { name: /submit application/i }).click();
  await expect.poll(() => attempts).toBeGreaterThanOrEqual(2);

  expect(keysSeen).toHaveLength(2);
  expect(keysSeen[0]).toBeTruthy();
  expect(keysSeen[1]).toBeTruthy();
  expect(keysSeen[1]).not.toBe(keysSeen[0]);
});

test("a resumable 503 still keeps the credentials", async ({ page }) => {
  // The other side of the rule above, and the one that must not regress.
  //
  // Clearing on every error would destroy the retry contract: an identity
  // verification outage is exactly the case where the NEXT submit must present
  // the SAME key and secret so it recovers the application already recorded,
  // instead of creating a second one.
  const applicant = fictionalApplicant("Resumable", true, 100_000);

  const keysSeen: string[] = [];
  let attempts = 0;

  await page.route("**/los/applications", async (route) => {
    const body = route.request().postDataJSON() as { idempotency_key?: string };
    keysSeen.push(body?.idempotency_key ?? "");
    attempts += 1;
    if (attempts === 1) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({
          detail: {
            error: "identity_verification_unavailable",
            message: "This application was recorded but not verified.",
            app_id: 4242,
          },
        }),
      });
      return;
    }
    await route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        app_id: 4242, status: "submitted", access_token: "acc-tok",
        kyc: { name_verified: true, dob_verified: true, address_verified: true, ssn_verified: true },
      }),
    });
  });

  await submitApplication(page, applicant, { stopAtReview: true });
  await page.getByRole("button", { name: /submit application/i }).click();
  await expect(page.locator(".alert-error").first()).toBeVisible();

  await page.getByRole("button", { name: /submit application/i }).click();
  await expect.poll(() => attempts).toBeGreaterThanOrEqual(2);

  expect(keysSeen[1]).toBe(keysSeen[0]);
});
