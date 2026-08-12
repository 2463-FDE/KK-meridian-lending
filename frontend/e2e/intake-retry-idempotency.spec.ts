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
