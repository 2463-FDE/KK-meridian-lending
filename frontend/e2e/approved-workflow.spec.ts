import { test, expect } from "@playwright/test";
import { fictionalApplicant, submitApplication, currentAppId, getDecision, dbClient, countRows } from "./fixtures";

test("borrower can apply, get approved, view the auto-generated offer without a duplicate-offer error, accept, and board", async ({ page }) => {
  const applicant = fictionalApplicant("Morgan", /* even ssn */ true, 100_000);
  await submitApplication(page, applicant);
  const appId = await currentAppId(page);

  await getDecision(page);
  await expect(page.getByText("Approve", { exact: true })).toBeVisible();

  // The offer view must retrieve run_decision's server-side auto-generated
  // offer, not attempt to create a second one -- assert directly on the
  // network traffic that no POST to /los/offer happens when a GET already
  // succeeds (a duplicate-create attempt is exactly the pre-fix behavior).
  let createCalls = 0;
  page.on("request", (req) => {
    if (req.method() === "POST" && req.url().includes("/los/offer")) createCalls++;
  });

  await page.getByRole("button", { name: /View your offer/ }).click();
  await expect(page.getByText(/FEDERAL TRUTH-IN-LENDING/i)).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText(/already been created/i)).not.toBeVisible();
  await expect(page.getByText(/^409$/)).not.toBeVisible();

  await page.getByRole("button", { name: "Accept offer" }).click();
  await expect(page.getByText("Offer accepted")).toBeVisible({ timeout: 15_000 });

  expect(createCalls, "the offer step must never POST-create when GET already found one").toBe(0);

  const client = dbClient();
  await client.connect();
  try {
    expect(await countRows(client, "applications", "id", appId)).toBe(1);
    expect(await countRows(client, "decisions", "app_id", appId)).toBe(1);
    expect(await countRows(client, "offers", "app_id", appId)).toBe(1);
    expect(await countRows(client, "loans", "app_id", appId)).toBe(1);

    const decisionRow = await client.query("SELECT outcome FROM decisions WHERE app_id = $1", [appId]);
    expect(decisionRow.rows[0].outcome).toBe("approve");

    const appRow = await client.query(
      "SELECT status, accept_token_hash FROM applications WHERE id = $1",
      [appId],
    );
    expect(appRow.rows[0].status).toBe("funded");
    expect(appRow.rows[0].accept_token_hash).toBeNull(); // consumed on boarding
  } finally {
    await client.end();
  }
});
