import { test, expect } from "@playwright/test";
import { fictionalApplicant, submitApplication, currentAppId, getDecision, dbClient } from "./fixtures";

test("an application whose offer was already auto-generated retrieves it instead of attempting to create a duplicate", async ({ page }) => {
  const applicant = fictionalApplicant("Casey", /* even ssn */ true, 100_000);
  await submitApplication(page, applicant);
  const appId = await currentAppId(page);
  await getDecision(page);
  await expect(page.getByText("Approve", { exact: true })).toBeVisible();

  // Arrange: wait for run_decision's own best-effort auto-generation to
  // land in Postgres BEFORE the browser ever asks for the offer -- this is
  // the exact race the fix targets (an offer that already exists by the
  // time the borrower reaches this step).
  const client = dbClient();
  await client.connect();
  try {
    await expect
      .poll(
        async () => {
          const res = await client.query("SELECT count(*)::int AS n FROM offers WHERE app_id = $1", [appId]);
          return res.rows[0].n;
        },
        { timeout: 15_000, message: "auto-generated offer never appeared in Postgres" },
      )
      .toBe(1);
  } finally {
    await client.end();
  }

  // Act: only now does the browser reach the offer step.
  let createCalls = 0;
  page.on("request", (req) => {
    if (req.method() === "POST" && req.url().includes("/los/offer")) createCalls++;
  });
  let sawExistingOfferGet = false;
  page.on("response", (res) => {
    if (res.request().method() === "GET" && res.url().includes("/offer") && res.status() === 200) {
      sawExistingOfferGet = true;
    }
  });

  await page.getByRole("button", { name: /View your offer/ }).click();
  await expect(page.getByText(/FEDERAL TRUTH-IN-LENDING/i)).toBeVisible({ timeout: 15_000 });

  expect(sawExistingOfferGet, "the existing offer must be retrieved via GET").toBe(true);
  expect(createCalls, "no duplicate-create POST may fire once the offer already exists").toBe(0);
});
