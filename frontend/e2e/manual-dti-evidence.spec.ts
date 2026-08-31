import { test, expect } from "@playwright/test";
import type { Page } from "@playwright/test";
import type { Client } from "pg";
import {
  REFER_BAND_INCOME,
  dbClient,
  fictionalApplicant,
  signInAsStaff,
  submitApplication,
  currentAppId,
  getDecision,
} from "./fixtures";

/**
 * RF-25 on screen: an underwriter records a manual DTI as EVIDENCE.
 *
 * The client authorised staff to apply DTI manually on a referred application,
 * as an underwriter or admin, from approved synthetic source documents --
 * recording income, obligations, the documents, the calculation, who assessed
 * it, their role, when and why. The constraint that shapes the whole feature is
 * that a manual DTI **decides nothing**.
 *
 * WHAT THIS FILE PROVES THAT THE API TESTS CANNOT
 *
 *   1. The panel exists where the work happens, and a reviewer can complete the
 *      round trip through the real gateway and database.
 *   2. Recording evidence changes NO decision surface -- read out of Postgres
 *      on both sides of the submission, not inferred from the screen.
 *   3. The ratio a reviewer SEES is the server's. There is no browser-side
 *      calculation: nothing resembling a percentage appears while the two
 *      figures are being typed, and the figure that does appear afterwards
 *      matches `manual_dti_assessments.dti_bp`.
 *   4. A CSR -- staff everywhere else on this page -- is not offered the form.
 *
 * FIXTURE STATE. This spec creates its OWN application through the intake
 * wizard rather than using a seeded one. `manual_dti_assessments` is
 * append-only (0047), so a row written against a shared application could never
 * be removed, and the decision-surface snapshot below would be comparing a
 * table other specs also write to. See RF-27 for the same reasoning applied to
 * `ledger_entries`.
 */

// Intake is five steps, then a decision, then a staff sign-in and a page load
// before the first assertion -- and this file pays the Next.js cold compile for
// the underwriting detail route.
test.describe.configure({ timeout: 180_000 });

async function withDb<T>(fn: (c: Client) => Promise<T>): Promise<T> {
  const client = dbClient();
  await client.connect();
  try {
    return await fn(client);
  } finally {
    await client.end();
  }
}

/** Every table that could carry a decision, as the API test reads them too. */
async function decisionSurface(client: Client, appId: string) {
  const [decisions, application, reviews, attempts] = await Promise.all([
    client.query("SELECT outcome FROM decisions WHERE app_id = $1", [appId]),
    client.query("SELECT status FROM applications WHERE id = $1", [appId]),
    client.query(
      "SELECT outcome, reason FROM manual_reviews WHERE app_id = $1",
      [appId]
    ),
    client.query(
      "SELECT id, state FROM decision_attempts WHERE app_id = $1 ORDER BY id",
      [appId]
    ),
  ]);
  return JSON.stringify({
    decisions: decisions.rows,
    application: application.rows,
    reviews: reviews.rows,
    attempts: attempts.rows,
  });
}

/** Create a referred application and return its id. */
async function aReferredApplication(page: Page): Promise<string> {
  // `false` is the SSN-parity flag every other refer-band spec passes
  // (`refer-staff-approve`, `decision-evidence`). The bureau stub keys its
  // score off that digit, so `true` scores out of the manual-review band and
  // the application comes back approved -- which the assertion below caught
  // rather than letting this spec silently test an approved application.
  const applicant = fictionalApplicant("Dti", false, REFER_BAND_INCOME);
  await submitApplication(page, applicant);
  const appId = await currentAppId(page);
  await getDecision(page);
  await withDb(async (client) => {
    const { rows } = await client.query(
      "SELECT outcome FROM decisions WHERE app_id = $1",
      [appId]
    );
    expect(
      rows[0]?.outcome,
      "the refer-band income no longer produces a referral, so this spec is " +
        "not testing what it says it is"
    ).toBe("refer");
  });
  return appId;
}

test("an underwriter records manual DTI evidence, and it decides nothing", async ({
  page,
}) => {
  const appId = await aReferredApplication(page);

  await signInAsStaff(page, "underwriter");
  await page.goto(`/underwriting/${appId}`);

  const panel = page.getByTestId("manual-dti");
  await expect(panel).toBeVisible({ timeout: 30_000 });

  // The panel says what it is before it says what to type. Recording evidence
  // sits directly under an approve/deny control, so the disclaimer is part of
  // the feature rather than decoration.
  await expect(panel).toContainText("evidence for a human reviewer");
  await expect(panel).toContainText("does not approve, deny or change");

  // The unapproved registry row exists (0047 seeds it so the refusal path has
  // something real to refuse) and must not be offered as a choice.
  await expect(panel).not.toContainText("SYN-DRAFT-001");

  const before = await withDb((c) => decisionSurface(c, appId));

  await page.locator("#dti-income").fill("6250.00");
  await page.locator("#dti-obligations").fill("2100.00");

  // NO BROWSER-SIDE CALCULATION. 2100/6250 is 33.60%, and with both figures
  // entered nothing on this panel may show it yet -- the recorded ratio comes
  // from the database, and a preview here would be a second definition of a
  // calculation the schema's CHECK constraint owns.
  await expect(panel).not.toContainText("33.60%");
  await expect(panel.getByTestId("manual-dti-ratio")).toHaveCount(0);

  await panel.getByText("SYN-PAYSTUB-001", { exact: false }).click();
  await panel.getByText("SYN-DEBTSCH-001", { exact: false }).click();
  await page
    .locator("#dti-reason")
    .fill("Income from the synthetic paystub; obligations from the schedule.");

  await panel.getByTestId("manual-dti-submit").click();

  const recorded = panel.getByTestId("manual-dti-recorded");
  await expect(recorded).toBeVisible({ timeout: 30_000 });
  await expect(recorded).toContainText("33.60%");

  // The figure on screen is the STORED one, not a coincidence of arithmetic.
  await withDb(async (client) => {
    const { rows } = await client.query(
      "SELECT dti_bp, assessed_role, gross_monthly_income, " +
        "       monthly_debt_obligations, reason " +
        "  FROM manual_dti_assessments WHERE app_id = $1",
      [appId]
    );
    expect(rows).toHaveLength(1);
    expect(rows[0].dti_bp).toBe(3360);
    expect(rows[0].assessed_role).toBe("underwriter");

    const { rows: docs } = await client.query(
      "SELECT d.doc_ref FROM manual_dti_assessment_documents l " +
        "  JOIN manual_dti_source_documents d ON d.id = l.document_id " +
        "  JOIN manual_dti_assessments a ON a.id = l.assessment_id " +
        " WHERE a.app_id = $1 ORDER BY d.doc_ref",
      [appId]
    );
    expect(docs.map((d) => d.doc_ref)).toEqual([
      "SYN-DEBTSCH-001",
      "SYN-PAYSTUB-001",
    ]);
  });

  // The constraint the whole feature is built around, read from the database
  // rather than inferred from the screen.
  const after = await withDb((c) => decisionSurface(c, appId));
  expect(
    after,
    "recording manual DTI evidence changed a decision surface"
  ).toBe(before);

  // And it is on the register, attributed, for the next reviewer to read.
  const entry = panel.getByTestId("manual-dti-assessment").first();
  await expect(entry).toContainText("33.60%");
  await expect(entry).toContainText("underwriter");
  await expect(entry).toContainText("SYN-PAYSTUB-001");
});

test("a CSR is not offered the form", async ({ page }) => {
  const appId = await aReferredApplication(page);

  await signInAsStaff(page, "csr");
  await page.goto(`/underwriting/${appId}`);

  // The page itself still loads for a CSR -- they are staff here. The manual
  // DTI panel is the part the client did not authorise them for, and offering a
  // form whose every submission would be refused is worse than offering none.
  //
  // ANCHORED ON SOMETHING THAT RENDERS AFTER THE PANEL WOULD HAVE. A negative
  // assertion is only worth anything once the thing it denies has had its
  // chance: the first version waited for the text "Application" and then
  // checked the panel was absent, and it PASSED against a deliberately broken
  // build that showed a CSR the form -- because it ran before the panel
  // rendered at all. The "Offer" heading sits directly below the panel in the
  // same component tree, so seeing it means the panel's own render decision has
  // already been made.
  await expect(page.getByRole("heading", { name: "Offer", exact: true }))
    .toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId("manual-dti")).toHaveCount(0);

  // And the page really is the one this test thinks it is -- an error page or a
  // redirect would also have no panel on it.
  await expect(page.getByText(`Application #${appId}`)).toBeVisible();
});
