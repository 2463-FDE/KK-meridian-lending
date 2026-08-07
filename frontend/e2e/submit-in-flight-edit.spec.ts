import { test, expect } from "@playwright/test";
import { fictionalApplicant, submitApplication, currentAppId, dbClient } from "./fixtures";

/**
 * The in-flight submission race, reproduced and then proven closed.
 *
 * Reviewed as high severity. `submitApplication` snapshotted `form` into the
 * POST body and set `busy`, but the review screen's Edit buttons were added
 * later and never honoured `busy` -- unlike the Submit and Back buttons beside
 * them. So a borrower could press Submit, immediately edit the loan amount
 * before the response returned, and land on Step 5 where the offer panel read
 * the MUTATED form: the displayed terms, and the terms sent to the fallback
 * offer creation, could differ from the application the backend had just
 * accepted.
 *
 * Reproducing it needs the response held open, which is what `page.route` does
 * below -- the window is invisibly short against a local API, so a test that
 * merely clicked fast would pass whether or not the bug existed.
 *
 * Three layers are asserted, because the fix has three and any one of them
 * alone would leave a gap:
 *   1. the Edit controls are disabled while the submission is in flight;
 *   2. the underlying navigation refuses even if a control is activated anyway;
 *   3. Step 5 reads an immutable submitted snapshot, so the terms shown and
 *      requested are the submitted ones regardless.
 */

// Wide enough that the in-flight assertions below evaluate comfortably inside
// the window. A short delay made them race the submission completing, so the
// test failed for a timing reason rather than a behavioural one -- which is a
// flaky test, not a strict one.
const DELAY_MS = 8000;

test("an edit attempted while the submission is in flight cannot change the application", async ({ page }) => {
  const applicant = fictionalApplicant("Sasha", /* even ssn */ true, 100_000);

  // Capture what was actually POSTed, and hold the response open so the
  // in-flight window is real and long enough to interact with. Asserting
  // against the captured body rather than against text scraped off the review
  // screen makes this a statement about the request, which is what the race
  // corrupts.
  const sent: Array<Record<string, unknown>> = [];
  await page.route("**/los/applications", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    try {
      sent.push(JSON.parse(route.request().postData() || "{}"));
    } catch {
      /* unparseable body is not an assertable body */
    }
    await new Promise((r) => setTimeout(r, DELAY_MS));
    return route.fallback();
  });

  // Fill and reach the review screen, but do not submit yet: submitApplication()
  // in fixtures.ts submits and waits, which is exactly what must not happen here.
  await submitApplication(page, applicant, { stopAtReview: true });

  await page.getByRole("button", { name: /Submit application/ }).click();

  // --- layer 1: every Edit control is disabled while in flight --------------
  for (const group of ["Personal", "Employment & income", "Loan details"]) {
    await expect(
      page.getByRole("button", { name: `Edit ${group}` }),
      `Edit ${group} must be disabled during submission`,
    ).toBeDisabled();
  }

  // --- layer 2: forcing the click through changes nothing -------------------
  // `force` bypasses the disabled-state actionability check, which is the
  // closest a browser test can get to "the control was activated anyway".
  await page.getByRole("button", { name: "Edit Loan details" }).click({ force: true });

  // Still on the review screen -- the navigation refused. Short timeouts on
  // purpose: these must hold IMMEDIATELY and while the request is still open,
  // so a generous timeout would let the assertion drift past the window and
  // start describing the post-submission screen instead.
  await expect(page.getByText("Step 4 of 5")).toBeVisible({ timeout: 1_000 });
  // The submit control relabels to "Submitting…" while busy, so matching it by
  // that label asserts two things at once: still on the review, and still
  // genuinely in flight rather than finished early.
  await expect(page.getByRole("button", { name: /Submitting/ }))
    .toBeVisible({ timeout: 1_000 });

  // The submission completes on its own terms.
  await expect(page.getByText(/Decision|Get decision/i).first()).toBeVisible({
    timeout: 20_000,
  });

  // --- layer 3: the record matches what was on the review screen ------------
  expect(sent.length, "exactly one application POST").toBe(1);

  // By id from the page, never max(id): specs run in parallel, so
  // ORDER BY id DESC can return another test's application.
  const appId = await currentAppId(page);
  const client = dbClient();
  await client.connect();
  try {
    const row = await client.query(
      "SELECT amount, term_months FROM applications WHERE id = $1",
      [appId],
    );
    expect(row.rowCount).toBe(1);
    // The stored record is what was sent -- the forced Edit changed nothing.
    expect(Number(row.rows[0].amount)).toBe(Number(sent[0].amount));
    expect(Number(row.rows[0].term_months)).toBe(Number(sent[0].term_months));
  } finally {
    await client.end();
  }
});

test("the offer is created on the submitted terms, not on later form state", async ({ page }) => {
  const applicant = fictionalApplicant("Alexis", /* even ssn */ true, 100_000);

  await page.route("**/los/applications", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    await new Promise((r) => setTimeout(r, DELAY_MS));
    return route.fallback();
  });

  await submitApplication(page, applicant, { stopAtReview: true });
  await page.getByRole("button", { name: /Submit application/ }).click();
  await page.getByRole("button", { name: "Edit Loan details" }).click({ force: true });

  // ACTUALLY ATTEMPT THE MUTATION. Forcing the click alone proves nothing: if
  // the edit screen opens but no value is changed, the offer terms are the same
  // either way and the test passes whether or not the defect is present. This
  // was true of an earlier draft of this test, and a mutation check caught it.
  //
  // Under the fix the term control is never on screen, so this is a no-op.
  // Under the defect it changes the term mid-flight, which is precisely the
  // corruption being tested for.
  // Selected by its options rather than by its label: Field renders a bare
  // <label> with no htmlFor and the control is not nested inside it, so
  // getByLabel matches nothing here. Filtering on a term-specific option value
  // identifies it unambiguously without changing app code for a test's benefit.
  const term = page
    .locator("select")
    .filter({ has: page.locator('option[value="60"]') });
  if (await term.count()) {
    await term.selectOption("60");
  }

  await expect(page.getByText(/Decision|Get decision/i).first()).toBeVisible({
    timeout: 20_000,
  });

  // Read the id now, while the intake confirmation is still the alert on
  // screen. Later panels replace it, and reading it then returned an id that
  // matched no row.
  const appId = await currentAppId(page);

  // Capture what the offer call actually asks for. This is the assertion that
  // would have caught the original defect at its most damaging point: an offer
  // created on terms the application record does not carry.
  const offerRequests: Array<Record<string, unknown>> = [];
  page.on("request", (req) => {
    if (req.method() === "POST" && req.url().includes("/los/offer")) {
      try {
        offerRequests.push(JSON.parse(req.postData() || "{}"));
      } catch {
        /* a body we cannot parse is not a body we can assert on */
      }
    }
  });

  await page.getByRole("button", { name: /Get decision/ }).click();
  await expect(page.getByText("Approve", { exact: true })).toBeVisible({ timeout: 20_000 });
  await page.getByRole("button", { name: /View your offer/ }).click();
  await expect(page.getByText(/FEDERAL TRUTH-IN-LENDING/i)).toBeVisible({ timeout: 20_000 });

  const client = dbClient();
  await client.connect();
  try {
    const row = await client.query(
      "SELECT id, amount, term_months FROM applications WHERE id = $1",
      [appId],
    );
    expect(row.rowCount).toBe(1);
    const app = row.rows[0];
    const offer = await client.query(
      "SELECT id FROM offers WHERE app_id = $1",
      [app.id],
    );
    expect(offer.rowCount).toBe(1);
    // Deliberately not asserting offers.term_months here: that column arrives
    // with PR #10's migration 0030 and does not exist on this branch. Writing
    // an assertion against a sibling branch's schema is how a test starts
    // failing for a reason that has nothing to do with what it tests.
    //
    // The offer panel states the terms to the borrower. Under the defect this
    // read mutable form state, so an in-flight edit changed what the borrower
    // was told their offer was for. It now reads the submitted snapshot.
    await expect(
      page.getByText(`over ${app.term_months} months`, { exact: false }),
    ).toBeVisible();

    // And if the fallback creation path DID run, it must have sent the
    // submitted terms. Usually it does not: the offer is auto-generated on
    // approval and the GET finds it, which approved-workflow.spec.ts asserts
    // separately. So this loop is conditional by nature -- asserting that a
    // POST happened would contradict that test.
    for (const body of offerRequests) {
      expect(Number(body.principal)).toBe(Number(app.amount));
      expect(Number(body.term_months)).toBe(Number(app.term_months));
    }
  } finally {
    await client.end();
  }
});
