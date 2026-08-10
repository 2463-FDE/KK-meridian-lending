import { test, expect } from "@playwright/test";
import {
  fictionalApplicant,
  submitApplication,
  currentAppId,
  getDecision,
  dbClient,
} from "./fixtures";

/**
 * Regenerating a legacy offer reprices it, and the borrower is told so.
 *
 * The warning used to read "Regenerating it records the exact terms -- your
 * amount, rate and term do not change." That was false. Regeneration runs the
 * offer through the current pricing policy: the note rate, the origination fee,
 * the APR, the monthly payment and the whole schedule are recalculated, and any
 * of them can come back different. Promising a borrower their terms are
 * unchanged and then changing them is precisely what a disclosure exists to
 * prevent, so the copy is the defect, not a wording preference.
 *
 * Two things are asserted, because either alone is passable by a broken page:
 *
 *   1. the pre-regeneration warning states that the offer will be recalculated
 *      and does NOT promise the rate is preserved;
 *   2. after regenerating, Accept is unavailable until the borrower confirms
 *      they have read the new disclosure.
 *
 * Reviewed on PR #10.
 */

test("the regeneration warning says the offer will be repriced, and acceptance waits for review", async ({
  page,
}) => {
  const applicant = fictionalApplicant("Odile", /* even ssn */ true, 96_000);
  await submitApplication(page, applicant);
  const appId = await currentAppId(page);
  await getDecision(page);

  // Make this application's offer the legacy shape the warning is for: an offer
  // that displays but cannot board, because it has no stored contractual
  // schedule. Stripped BEFORE it is first viewed -- the wizard is client-side
  // state, so reloading to pick up a database change would drop the borrower
  // back to step 1 with no "View your offer" button at all.
  const client = dbClient();
  await client.connect();
  try {
    const stripped = await client.query(
      // All SIX contractual columns. `offers_schedule_all_or_nothing` treats
      // them as one fact, so clearing five and leaving `term_months` set is
      // rejected -- which is the constraint working, and exactly the partial
      // state this PR made unrepresentable.
      "UPDATE offers SET regular_payment_count = NULL, final_payment = NULL, " +
      "term_months = NULL, schedule_version = NULL, principal = NULL, " +
      "note_rate_pct = NULL " +
      "WHERE app_id = $1 AND accepted_at IS NULL RETURNING id",
      [appId],
    );
    expect(stripped.rowCount, "the application should have an unaccepted offer").toBe(1);
  } finally {
    await client.end();
  }

  await page.getByRole("button", { name: /View your offer/ }).click();
  await expect(page.getByText(/FEDERAL TRUTH-IN-LENDING/i)).toBeVisible({ timeout: 15_000 });

  // 1. The warning, and what it must not claim.
  const warning = page.getByTestId("offer-not-boardable");
  await expect(warning).toBeVisible({ timeout: 15_000 });
  await expect(warning).toContainText(/recalculated/i);
  await expect(warning).toContainText(/interest rate/i);
  await expect(warning).toContainText(/origination fee/i);
  // The retracted promise, asserted as an absence. This is the sentence that was
  // wrong, so its return is what this test exists to catch.
  await expect(warning).not.toContainText(/rate and term do not change/i);
  await expect(warning).not.toContainText(/terms? (?:do|does) not change/i);

  // Accept is not offered in this state at all -- it would 409.
  await expect(page.getByRole("button", { name: "Accept offer" })).toHaveCount(0);

  // 2. Regenerate, then the review gate.
  await page.getByTestId("regenerate-offer").click();

  const repriced = page.getByTestId("repriced-disclosure");
  await expect(repriced).toBeVisible({ timeout: 20_000 });
  await expect(repriced).toContainText(/these are new terms/i);

  const accept = page.getByRole("button", { name: "Accept offer" });
  await expect(accept).toBeVisible();
  await expect(accept, "acceptance must wait for the borrower to review").toBeDisabled();

  await page.getByTestId("ack-new-terms").check();
  await expect(accept).toBeEnabled();

  // And the acceptance still works, so the gate is a gate and not a wall.
  await accept.click();
  await expect(page.getByText("Offer accepted")).toBeVisible({ timeout: 20_000 });
});
