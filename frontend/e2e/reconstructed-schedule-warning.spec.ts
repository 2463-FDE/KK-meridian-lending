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
  // Seeded loans predate the Model B columns: db/init inserts them without
  // regular_payment/final_payment/schedule_version, which is exactly the
  // legacy shape. Picked from the database rather than hard-coded so this does
  // not break when seed ids change.
  const client = dbClient();
  await client.connect();
  let legacyLoanId: number;
  try {
    const row = await client.query(
      "SELECT id FROM loans WHERE schedule_version IS NULL ORDER BY id LIMIT 1",
    );
    expect(row.rowCount, "the seed should contain at least one pre-0030 loan").toBe(1);
    legacyLoanId = row.rows[0].id;
  } finally {
    await client.end();
  }

  await signInAsStaff(page);
  await page.goto(`/servicing/${legacyLoanId}`);

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
