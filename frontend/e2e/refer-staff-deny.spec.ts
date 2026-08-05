import { test, expect } from "@playwright/test";
import {
  fictionalApplicant, submitApplication, currentAppId, getDecision,
  signInAsStaff, resolveReferAsStaff, dbClient, countRows, REFER_BAND_INCOME,
} from "./fixtures";

/**
 * Gap G (PR #6 review), the other half of the manual-review path:
 *
 * borrower applies -> automated REFER -> staff denies with a required reason
 * -> the denial reason is recorded and attributable -> no offer exists -> no
 * loan exists. Postgres rows are checked directly.
 *
 * The negative assertions are the point: a staff denial must leave the
 * application with nothing a borrower could act on -- no offer to view, no
 * acceptance token to spend, no loan.
 */
test("REFER denied by staff records the reason and creates no offer and no loan", async ({ page }) => {
  const applicant = fictionalApplicant("Casey", /* even ssn */ false, REFER_BAND_INCOME);
  await submitApplication(page, applicant);
  const appId = await currentAppId(page);

  await getDecision(page);
  await expect(page.getByText("Refer", { exact: true })).toBeVisible();

  const client = dbClient();
  await client.connect();
  try {
    expect((await client.query("SELECT outcome FROM decisions WHERE app_id = $1", [appId])).rows[0].outcome)
      .toBe("refer");

    const reason = "Employment could not be verified with the stated employer";
    await signInAsStaff(page);
    await resolveReferAsStaff(page, appId, "deny", reason);

    // --- the denial is the decision of record, and it is attributable ------
    const decision = await client.query("SELECT outcome FROM decisions WHERE app_id = $1", [appId]);
    expect(decision.rows[0].outcome).toBe("deny");

    const review = await client.query(
      "SELECT outcome, reason, reviewer_role FROM manual_reviews WHERE app_id = $1",
      [appId],
    );
    expect(review.rowCount).toBe(1);
    expect(review.rows[0].outcome).toBe("deny");
    expect(review.rows[0].reason).toBe(reason);
    expect(review.rows[0].reviewer_role).toBe("underwriter");

    // The recorded reason is shown back on the staff screen, not silently kept.
    // (.first(): it renders in both the finalized-decision panel and the
    // adverse-action line, which is itself the correct behaviour.)
    await expect(page.getByText(reason).first()).toBeVisible();

    // --- nothing actionable exists -----------------------------------------
    expect(await countRows(client, "offers", "app_id", appId)).toBe(0);
    expect(await countRows(client, "loans", "app_id", appId)).toBe(0);

    const app = await client.query(
      "SELECT status, accept_token_hash FROM applications WHERE id = $1",
      [appId],
    );
    expect(app.rows[0].status).not.toBe("funded");
    // A denied application must never hold a live acceptance credential.
    expect(app.rows[0].accept_token_hash).toBeNull();

    // Staff cannot generate an offer for a denied application either.
    await page.goto(`/underwriting/${appId}`);
    await expect(page.getByRole("button", { name: /Make offer/ })).toBeDisabled();
  } finally {
    await client.end();
  }
});
