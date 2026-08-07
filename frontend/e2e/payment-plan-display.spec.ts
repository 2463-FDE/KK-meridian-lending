import { test, expect } from "@playwright/test";
import { fictionalApplicant, submitApplication, currentAppId, getDecision, dbClient } from "./fixtures";

/**
 * Model B payment presentation, borrower-facing.
 *
 * The offer screen used to say "monthly payment $X" beside a term, which told
 * the borrower they would make N identical payments. Under Model B they will
 * not: the final period bills an adjusted amount that absorbs the cent residue,
 * and it is a different number in almost every schedule.
 *
 * The expected strings below are the RV-2-36 golden vector
 * (db/golden/model_b_schedule_vectors.json): 15,000 at 7.99% over 36 months
 * bills 469.98 for 35 periods and 469.87 in the last one. Those are literals
 * verified by hand, so this test fails if the generator, the API field
 * plumbing, or the sentence construction changes -- not merely if they
 * disagree with each other.
 *
 * The apply form defaults to exactly that principal and term, which is why this
 * vector is the one asserted here.
 */

const REGULAR = "$469.98";
const FINAL = "$469.87";
const REGULAR_COUNT = 35;

test("the offer states the regular payments and the different final payment, not one uniform amount", async ({ page }) => {
  const applicant = fictionalApplicant("Devon", /* even ssn */ true, 100_000);
  await submitApplication(page, applicant);
  const appId = await currentAppId(page);

  await getDecision(page);
  await expect(page.getByText("Approve", { exact: true })).toBeVisible();

  await page.getByRole("button", { name: /View your offer/ }).click();
  await expect(page.getByText(/FEDERAL TRUTH-IN-LENDING/i)).toBeVisible({ timeout: 15_000 });

  // The whole sentence, in one assertion: a partial match would pass on
  // "35 monthly payments of $469.98" with the final payment silently dropped,
  // which is the defect.
  await expect(
    page.getByText(
      `${REGULAR_COUNT} monthly payments of ${REGULAR}, then a final payment of ${FINAL}`,
    ),
  ).toBeVisible({ timeout: 15_000 });

  // And the final payment is genuinely a different number from the regular one,
  // so this test cannot pass on a schedule where the distinction was collapsed.
  expect(REGULAR).not.toBe(FINAL);

  // The stored contract must match what the borrower was just shown. Read from
  // Postgres rather than the API so the assertion is against the persisted row.
  const client = dbClient();
  await client.connect();
  try {
    const offer = await client.query(
      "SELECT monthly_payment, regular_payment_count, final_payment, term_months, schedule_version "
      + "FROM offers WHERE app_id = $1",
      [appId],
    );
    expect(offer.rowCount).toBe(1);
    const row = offer.rows[0];
    expect(Number(row.monthly_payment)).toBe(469.98);
    expect(Number(row.final_payment)).toBe(469.87);
    expect(Number(row.regular_payment_count)).toBe(REGULAR_COUNT);
    expect(Number(row.term_months)).toBe(REGULAR_COUNT + 1);
    expect(row.schedule_version).toBe("B1");
  } finally {
    await client.end();
  }
});

test("boarding copies the displayed payment plan onto the loan", async ({ page }) => {
  const applicant = fictionalApplicant("Rowan", /* even ssn */ true, 100_000);
  await submitApplication(page, applicant);
  const appId = await currentAppId(page);

  await getDecision(page);
  await page.getByRole("button", { name: /View your offer/ }).click();
  await expect(page.getByText(/FEDERAL TRUTH-IN-LENDING/i)).toBeVisible({ timeout: 15_000 });
  await page.getByRole("button", { name: "Accept offer" }).click();
  await expect(page.getByText("Offer accepted")).toBeVisible({ timeout: 15_000 });

  // Servicing bills these amounts, so the funded loan must carry the same terms
  // the borrower accepted -- not principal/rate/term for servicing to re-solve.
  const client = dbClient();
  await client.connect();
  try {
    const loan = await client.query(
      "SELECT apr, term_months, regular_payment, regular_payment_count, final_payment, "
      + "schedule_version FROM loans WHERE app_id = $1",
      [appId],
    );
    expect(loan.rowCount).toBe(1);
    const row = loan.rows[0];
    expect(Number(row.regular_payment)).toBe(469.98);
    expect(Number(row.final_payment)).toBe(469.87);
    expect(Number(row.regular_payment_count)).toBe(REGULAR_COUNT);
    expect(Number(row.term_months)).toBe(REGULAR_COUNT + 1);
    expect(row.schedule_version).toBe("B1");
    // The boarded rate is the contractual NOTE rate, not the disclosed APR.
    // 7.99 vs 10.072 for this vector, so a confusion between them fails here.
    expect(Number(row.apr)).toBe(7.99);
  } finally {
    await client.end();
  }
});
