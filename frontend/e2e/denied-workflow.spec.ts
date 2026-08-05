import { test, expect } from "@playwright/test";
import { fictionalApplicant, submitApplication, currentAppId, getDecision, dbClient, countRows } from "./fixtures";

test("a deterministically denied application shows the reason, offers no Make Offer action, and creates no offer or loan", async ({ page }) => {
  // Odd-ending SSN + very low income -> deny band (decision-service's stub
  // scoring: bureau_score = 612 for an odd SSN, well under the deny
  // threshold once income is this low).
  const applicant = fictionalApplicant("Jordan", /* even ssn */ false, 5_000);
  await submitApplication(page, applicant);
  const appId = await currentAppId(page);

  await getDecision(page);
  await expect(page.getByText("Deny", { exact: true })).toBeVisible();
  await expect(page.getByText(/adverse action reason/i)).toBeVisible();

  await expect(page.getByRole("button", { name: /View your offer/ })).not.toBeVisible();

  const client = dbClient();
  await client.connect();
  try {
    const decisionRow = await client.query("SELECT outcome FROM decisions WHERE app_id = $1", [appId]);
    expect(decisionRow.rows[0].outcome).toBe("deny");
    expect(await countRows(client, "offers", "app_id", appId)).toBe(0);
    expect(await countRows(client, "loans", "app_id", appId)).toBe(0);

    const appRow = await client.query("SELECT status, accept_token_hash FROM applications WHERE id = $1", [appId]);
    expect(appRow.rows[0].status).not.toBe("funded");
    expect(appRow.rows[0].accept_token_hash).toBeNull();
  } finally {
    await client.end();
  }
});
