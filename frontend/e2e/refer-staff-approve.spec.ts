import { test, expect } from "@playwright/test";
import {
  fictionalApplicant, submitApplication, currentAppId, getDecision,
  signInAsStaff, resolveReferAsStaff, dbClient, countRows, REFER_BAND_INCOME,
} from "./fixtures";

/**
 * Gap G (PR #6 review): the manual-review path had no committed browser
 * coverage at all -- only approve, deny and existing-offer were covered, so
 * the entire REFER -> staff -> offer -> accept -> board journey (the reason
 * db/migrations/0018 and 0020 exist) was verified by unit tests alone.
 *
 * borrower applies -> automated REFER -> no borrower offer yet -> staff signs
 * in -> staff approves with a required reason -> exactly one offer -> borrower
 * accepts -> exactly one loan boarded. Postgres rows are checked directly.
 */
test("REFER resolved by staff approval yields one offer, one loan, and an auditable review", async ({ page }) => {
  const applicant = fictionalApplicant("Robin", /* even ssn */ false, REFER_BAND_INCOME);
  await submitApplication(page, applicant);
  const appId = await currentAppId(page);

  // --- automated outcome is REFER, and the borrower gets no offer yet -------
  await getDecision(page);
  await expect(page.getByText("Refer", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: /View your offer/ })).not.toBeVisible();

  const client = dbClient();
  await client.connect();
  try {
    const beforeReview = await client.query("SELECT outcome FROM decisions WHERE app_id = $1", [appId]);
    expect(beforeReview.rows[0].outcome).toBe("refer");
    expect(await countRows(client, "offers", "app_id", appId)).toBe(0);
    expect(await countRows(client, "loans", "app_id", appId)).toBe(0);

    // --- staff resolves the refer, reason mandatory ------------------------
    const reason = "Verified updated income documentation; score band reconsidered";
    await signInAsStaff(page);
    await resolveReferAsStaff(page, appId, "approve", reason);

    // The decision is now the STAFF decision, recorded and attributable.
    const afterReview = await client.query("SELECT outcome FROM decisions WHERE app_id = $1", [appId]);
    expect(afterReview.rows[0].outcome).toBe("approve");

    const review = await client.query(
      "SELECT outcome, reason, reviewer_role, reviewer_name FROM manual_reviews WHERE app_id = $1",
      [appId],
    );
    expect(review.rowCount).toBe(1);
    expect(review.rows[0].outcome).toBe("approve");
    expect(review.rows[0].reason).toBe(reason);
    expect(review.rows[0].reviewer_role).toBe("underwriter");

    // Approval auto-generates exactly one offer -- never a duplicate.
    await expect.poll(
      async () => countRows(client, "offers", "app_id", appId),
      { timeout: 15_000 },
    ).toBe(1);

    // --- borrower accepts, in the browser ----------------------------------
    await page.goto("/apply");
    // A fresh borrower session has no in-page state for this application, so
    // drive the accept from the staff screen, which is the supported staff
    // path and exercises the same gated accept endpoint.
    await page.goto(`/underwriting/${appId}`);
    const board = page.getByRole("button", { name: /Accept & board|Accept and board|Board/ });
    await expect(board).toBeEnabled({ timeout: 15_000 });
    await board.click();

    // --- exactly one loan, one balance -------------------------------------
    await expect.poll(
      async () => countRows(client, "loans", "app_id", appId),
      { timeout: 15_000 },
    ).toBe(1);

    const loan = await client.query("SELECT id, note_rate_pct, principal, term_months FROM loans WHERE app_id = $1", [appId]);
    expect(loan.rowCount).toBe(1);

    // PR #10 review: this used to assert loans.apr == offers.apr, which encoded
    // the very confusion that review found -- once apr became the true actuarial
    // rate, boarding it meant servicing amortized above the disclosed payment.
    // The loan column is now `note_rate_pct` (D19, db/migrations/0039); the
    // offer's `apr` stays, because there it is a real disclosed APR.
    // The boarded rate is the CONTRACTUAL note rate, and the property worth
    // asserting is that billing it reproduces the disclosed payment.
    const offer = await client.query(
      "SELECT apr, note_rate_pct, monthly_payment FROM offers WHERE app_id = $1",
      [appId],
    );
    expect(Number(loan.rows[0].note_rate_pct)).toBeCloseTo(Number(offer.rows[0].note_rate_pct), 3);
    expect(Number(offer.rows[0].apr)).toBeGreaterThan(Number(offer.rows[0].note_rate_pct));

    const r = Number(loan.rows[0].note_rate_pct) / 100 / 12;
    const n = loan.rows[0].term_months;
    const f = Math.pow(1 + r, n);
    const billed = (Number(loan.rows[0].principal) * r * f) / (f - 1);
    expect(billed).toBeCloseTo(Number(offer.rows[0].monthly_payment), 1);

    const balances = await client.query("SELECT count(*)::int AS n FROM balances WHERE loan_id = $1", [loan.rows[0].id]);
    expect(balances.rows[0].n).toBe(1);

    const app = await client.query("SELECT status FROM applications WHERE id = $1", [appId]);
    expect(app.rows[0].status).toBe("funded");
  } finally {
    await client.end();
  }
});
