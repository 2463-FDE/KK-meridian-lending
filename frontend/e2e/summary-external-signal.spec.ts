import { test, expect } from "@playwright/test";
import { fictionalApplicant, submitApplication, currentAppId, signInAsStaff } from "./fixtures";

/**
 * The grounded external signal has to reach the officer.
 *
 * Reviewed as high severity on PR #13: `loan-assistant` composed the BLS
 * citation server-side and returned it in `external_signals`, but the only
 * officer-facing consumer -- `components/LoanSummaryCard.tsx` -- neither
 * declared the field nor rendered it. So the whole feature terminated at the
 * API boundary: an officer saw model-authored prose and flags, and the one
 * checkable, externally-published figure in the response was dropped on the
 * floor. A backend test asserting the field is present cannot catch that; only
 * something that looks at the rendered page can.
 *
 * The summary response is stubbed rather than generated. Three reasons, all of
 * which would otherwise make this test measure the wrong thing:
 *   - a real summary needs a licensed model, which CI has no key for;
 *   - the macro provider is deliberately disabled in tests, so a real response
 *     would carry an EMPTY external_signals and the assertion would be vacuous;
 *   - stubbing pins the exact citation string, so the test proves the server's
 *     text is displayed verbatim rather than reassembled in the browser.
 */

const CITATION =
  "U.S. Bureau of Labor Statistics: unemployment rate 4.2% (June 2026, series LNS14000000)";
const SIGNAL_URL = "https://data.bls.gov/timeseries/LNS14000000";

const STUB_SUMMARY = {
  applicant_name: "Officer Test Applicant",
  loan_amount: 18000,
  term_months: 48,
  purpose: "debt consolidation",
  risk_tier: "medium",
  summary: "Stable employment and adequate income for the requested amount.",
  flags: ["Debt-to-income near the policy limit"],
  external_signals: [
    {
      source: "U.S. Bureau of Labor Statistics",
      series_id: "LNS14000000",
      label: "unemployment rate",
      value: 4.2,
      unit: "%",
      period: "June 2026",
      url: SIGNAL_URL,
      citation: CITATION,
    },
  ],
};

test("the officer summary renders the server-composed external signal", async ({ page }) => {
  const applicant = fictionalApplicant("Nadia", /* even ssn */ true, 82_000);
  await submitApplication(page, applicant);
  const appId = await currentAppId(page);

  await signInAsStaff(page);

  await page.route("**/assistant/applications/*/summary", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(STUB_SUMMARY),
    });
  });

  await page.goto(`/underwriting/${appId}`);
  await page.getByRole("button", { name: /Generate AI Summary/ }).click();

  // The model's half still renders -- this must not be a test that passes by
  // replacing one section with another.
  await expect(page.getByText(STUB_SUMMARY.summary)).toBeVisible({ timeout: 15_000 });

  // The grounded half, verbatim. Asserting the exact string is the point: the
  // citation is composed by the server from what the provider returned, so a
  // browser-side reconstruction from the parts would be a second author on a
  // figure whose whole value is that it was published elsewhere.
  await expect(page.getByText(CITATION)).toBeVisible();

  // Labelled as not model-generated, so an officer can tell the checkable
  // figure apart from the prose above it.
  await expect(page.getByText(/External context \(not model-generated\)/i)).toBeVisible();

  // And it is checkable: the link points at the series the citation names.
  const verify = page.getByRole("link", { name: /Verify at U\.S\. Bureau of Labor Statistics/ });
  await expect(verify).toHaveAttribute("href", SIGNAL_URL);
});

test("a summary with no external signal renders no empty context section", async ({ page }) => {
  /* The provider fails open by design -- disabled, unreachable or rate-limited
   * all yield an empty list. An empty "External context" heading would imply
   * something had been withheld, which is a worse answer than saying nothing.
   */
  const applicant = fictionalApplicant("Owen", /* even ssn */ true, 82_000);
  await submitApplication(page, applicant);
  const appId = await currentAppId(page);

  await signInAsStaff(page);

  await page.route("**/assistant/applications/*/summary", async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ ...STUB_SUMMARY, external_signals: [] }),
    });
  });

  await page.goto(`/underwriting/${appId}`);
  await page.getByRole("button", { name: /Generate AI Summary/ }).click();

  await expect(page.getByText(STUB_SUMMARY.summary)).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText(/External context/i)).toHaveCount(0);
});
