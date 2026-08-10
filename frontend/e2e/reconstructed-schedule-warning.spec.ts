import { test, expect } from "@playwright/test";
import {
  fictionalApplicant,
  submitApplication,
  currentAppId,
  getDecision,
  dbClient,
  signInAsStaff,
} from "./fixtures";

/**
 * A reconstructed schedule must never be presented as the contractual one.
 *
 * Servicing reports `source` ("contract" | "reconstructed") and a `note` on
 * every schedule response. A loan boarded before db/migrations/0030 has no
 * stored contractual terms -- 0030 deliberately does not back-fill them,
 * because solving them again today would persist a guess as the agreed terms --
 * so its schedule is reconstructed from principal, rate and term with the
 * current generator.
 *
 * The server has said so since the Model B work. The servicing page ignored it
 * and rendered both kinds identically under the heading "Amortization
 * schedule", so a reader could not tell an estimate from a contract. That is
 * the one thing the server went to the trouble of saying.
 *
 * The two cases are asserted as a PAIR. Asserting only that the warning appears
 * would pass on a page that shows it unconditionally, which would be its own
 * defect -- it would train staff to ignore the banner on every loan they open.
 */

test("a legacy loan's schedule is labelled as reconstructed, not contractual", async ({ page }) => {
  // The legacy shape is CREATED here rather than found in the seed.
  //
  // This used to pick whichever seeded loan had no stored schedule, which was
  // all of them -- db/init's loan inserts predate the Model B columns. That is
  // no longer true: the seed now copies each loan's contractual schedule from
  // its own offer, because leaving them null made every demo loan look legacy
  // and hid its note rate. So the test makes its own subject by clearing the
  // columns on one high-id bulk loan, which is filler data no other spec
  // asserts on. Depending on a seed's shape for a fixture was the coupling
  // that broke here.
  const client = dbClient();
  await client.connect();
  let legacyLoanId: number;
  try {
    const row = await client.query(
      "UPDATE loans SET regular_payment = NULL, regular_payment_count = NULL, " +
      "final_payment = NULL, schedule_version = NULL " +
      "WHERE id = (SELECT max(id) FROM loans) RETURNING id",
    );
    expect(row.rowCount, "the seed should contain at least one loan").toBe(1);
    legacyLoanId = row.rows[0].id;
  } finally {
    await client.end();
  }

  await signInAsStaff(page);
  // Wait for the schedule RESPONSE, not just for the element.
  //
  // The page fetches the schedule best-effort (Promise.allSettled), so a slow
  // or failed request leaves the note unrendered with no retry -- and a test
  // that only waits on the DOM then reports "element not found" for what is
  // really an unanswered request. Asserting the response makes the two
  // distinguishable, and removes the race that made this intermittent in a
  // full-suite run while passing in isolation.
  const [scheduleResponse] = await Promise.all([
    page.waitForResponse(
      (r: { url: () => string }) =>
        r.url().includes(`/loans/${legacyLoanId}/schedule`),
      { timeout: 30_000 },
    ),
    page.goto(`/servicing/${legacyLoanId}`),
  ]);
  expect(scheduleResponse.ok(), "the schedule endpoint did not answer").toBe(true);
  const payload = await scheduleResponse.json();
  expect(payload.source, "this loan should have no stored schedule").toBe("reconstructed");

  const note = page.getByTestId("schedule-note");
  await expect(note).toBeVisible({ timeout: 15_000 });
  await expect(note).toContainText("These are not the agreed terms.");
  await expect(note).toContainText(/reconstructed/i);

  // The heading itself carries the qualification, so a screenshot or a printed
  // page cannot separate the table from its caveat.
  await expect(
    page.getByRole("heading", { name: /Amortization schedule \(reconstructed\)/ }),
  ).toBeVisible();

  // And the warning is outside the collapsed section -- a caveat behind a
  // "Show schedule" click is a caveat that gets missed.
  await expect(note).toBeVisible();
});

test("a loan boarded with its contractual schedule shows no such warning", async ({ page }) => {
  const applicant = fictionalApplicant("Nima", /* even ssn */ true, 100_000);
  await submitApplication(page, applicant);
  const appId = await currentAppId(page);

  await getDecision(page);
  await page.getByRole("button", { name: /View your offer/ }).click();
  await expect(page.getByText(/FEDERAL TRUTH-IN-LENDING/i)).toBeVisible({ timeout: 15_000 });
  await page.getByRole("button", { name: "Accept offer" }).click();
  await expect(page.getByText("Offer accepted")).toBeVisible({ timeout: 15_000 });

  const client = dbClient();
  await client.connect();
  let loanId: number;
  try {
    const row = await client.query(
      "SELECT id, schedule_version FROM loans WHERE app_id = $1",
      [appId],
    );
    expect(row.rowCount).toBe(1);
    // Precondition: this loan really did board with a stored schedule. Without
    // it the assertion below would pass for the wrong reason.
    expect(row.rows[0].schedule_version).toBe("B1");
    loanId = row.rows[0].id;
  } finally {
    await client.end();
  }

  await signInAsStaff(page);
  await page.goto(`/servicing/${loanId}`);
  await expect(page.getByRole("heading", { name: "Amortization schedule" })).toBeVisible({
    timeout: 15_000,
  });

  await expect(page.getByTestId("schedule-note")).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: /reconstructed/ }),
  ).toHaveCount(0);
});
