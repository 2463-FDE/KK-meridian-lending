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
 * Offer disclosure presentation.
 *
 * Reported from a screenshot of the staff offer screen. Four separate defects,
 * none of them in the money math -- every figure was already correct:
 *
 *  1. The contractual 7.99% was called an "APR". It is the NOTE RATE. The APR
 *     is a different, larger number because it carries the prepaid origination
 *     fee, and using one word for both is what let a 5.43% "APR" sit under a
 *     7.99% loan without looking wrong.
 *  2. Only one rate was shown, so the two could not be compared.
 *  3. The payment-plan text was a bare <p> appended inside `.tila`, which has
 *     overflow:hidden and no padding -- so it sat on the 2px border and
 *     clipped.
 *  4. "Offer already created" rendered as a disabled button: an unavailable
 *     ACTION rather than a statement of state.
 *
 * These tests assert presentation only. The financial values are covered by
 * payment-plan-display.spec.ts and the backend suites, and nothing here
 * recomputes them -- every expected figure is read from the database, so this
 * file cannot start disagreeing with the calculations it is not testing.
 */

const RATE_WORDS = /\b(APR|annual percentage rate)\b/i;

async function offerFor(page: { }, appId: string) {
  const client = dbClient();
  await client.connect();
  try {
    const row = await client.query(
      "SELECT note_rate_pct, apr, monthly_payment, final_payment, "
      + "regular_payment_count FROM offers WHERE app_id = $1",
      [appId],
    );
    expect(row.rowCount).toBe(1);
    return row.rows[0];
  } finally {
    await client.end();
  }
}

/** Reach an approved application with an offer, as the borrower. */
async function borrowerOffer(page: any) {
  const applicant = fictionalApplicant("Imani", /* even ssn */ true, 100_000);
  await submitApplication(page, applicant);
  const appId = await currentAppId(page);
  await getDecision(page);
  await page.getByRole("button", { name: /View your offer/ }).click();
  await expect(page.getByText(/FEDERAL TRUTH-IN-LENDING/i)).toBeVisible({ timeout: 15_000 });
  return appId;
}

// --- 1. terminology ----------------------------------------------------------

test("the staff offer prompt calls 7.99% a note rate, never an APR", async ({ page }) => {
  await signInAsStaff(page);
  const applicant = fictionalApplicant("Teo", /* even ssn */ true, 100_000);
  await submitApplication(page, applicant);
  const appId = await currentAppId(page);
  await getDecision(page);

  await page.goto(`/underwriting/${appId}`);
  const prompt = page.getByText(/Generate a Truth-in-Lending offer/);
  await expect(prompt).toBeVisible({ timeout: 15_000 });

  await expect(prompt).toContainText("using a 7.99% note rate");
  const text = (await prompt.textContent()) ?? "";
  expect(text, "the offer prompt must not call the note rate an APR").not.toMatch(RATE_WORDS);
});

// The borrower's step-3 rate estimate ("Estimated interest rate (note rate)
// 7.99% ... your APR will be higher") is changed in this commit but is NOT
// browser-covered here. Reaching step 3 without submitting needs the
// `stopAtReview` fixture option, which lives on PR #14's branch; adding it here
// too would land the same change on two branches, which is the cross-branch
// duplication CLAUDE.md exists to prevent. Covered by typecheck and build only
// until #14 merges, at which point this file should gain the assertion.

// --- 2. both rates, distinctly ----------------------------------------------

test("both the note rate and the federal APR are visible and different", async ({ page }) => {
  const appId = await borrowerOffer(page);
  const row = await offerFor(page, appId);

  const note = Number(row.note_rate_pct);
  const apr = Number(row.apr);
  expect(note).toBe(7.99);
  expect(apr).toBeGreaterThan(note);

  await expect(page.getByTestId("rate-summary")).toBeVisible();
  await expect(page.getByTestId("note-rate")).toHaveText(`${note.toFixed(2)}%`);
  await expect(page.getByTestId("federal-apr")).toHaveText(`${apr.toFixed(2)}%`);

  // The two summary values are genuinely different on screen, not the same
  // figure printed twice under two labels.
  const noteText = await page.getByTestId("note-rate").textContent();
  const aprText = await page.getByTestId("federal-apr").textContent();
  expect(noteText).not.toBe(aprText);
});

test("the federal cell keeps its official label and explains what it includes", async ({ page }) => {
  await borrowerOffer(page);

  await expect(page.getByText("Annual Percentage Rate", { exact: true })).toBeVisible();
  await expect(
    page.getByText(/The total cost of your credit as a yearly rate, including the origination fee/i),
  ).toBeVisible();
});

// --- 3. the payment schedule sits inside the disclosure ----------------------

test("the payment schedule renders inside the disclosure box", async ({ page }) => {
  const appId = await borrowerOffer(page);
  const row = await offerFor(page, appId);

  const schedule = page.getByTestId("payment-schedule");
  await expect(schedule).toBeVisible();

  // Structurally inside `.tila`, not merely beneath it on screen.
  const insideBox = await schedule.evaluate((el) => Boolean(el.closest(".tila")));
  expect(insideBox, "the payment schedule must be inside the disclosure box").toBe(true);

  await expect(schedule).toContainText("Payment schedule");
  await expect(schedule).toContainText(
    `${row.regular_payment_count} monthly payments of $${Number(row.monthly_payment).toFixed(2)}`,
  );
  await expect(schedule).toContainText(
    `followed by one final payment of $${Number(row.final_payment).toFixed(2)}`,
  );
});

test("the schedule text stays inside the disclosure border", async ({ page }) => {
  await borrowerOffer(page);

  const box = page.locator(".tila");
  const schedule = page.getByTestId("payment-schedule");
  const outer = await box.boundingBox();
  const inner = await schedule.boundingBox();
  expect(outer).not.toBeNull();
  expect(inner).not.toBeNull();

  // The reported defect: text overlapping the bottom border. Allowing 2px for
  // the border itself, the schedule row must sit within its container on every
  // edge.
  expect(inner!.y).toBeGreaterThanOrEqual(outer!.y - 1);
  expect(inner!.x).toBeGreaterThanOrEqual(outer!.x - 1);
  expect(inner!.y + inner!.height).toBeLessThanOrEqual(outer!.y + outer!.height + 1);
  expect(inner!.x + inner!.width).toBeLessThanOrEqual(outer!.x + outer!.width + 1);
});

test("a singular regular payment reads '1 monthly payment', not '1 payments'", async () => {
  // Pure formatter behaviour, so it needs no browser: the plural rule is the
  // thing under test, and reaching a one-period loan through the UI would test
  // the wizard instead.
  const { paymentPlanText } = await import("../lib/format");
  expect(paymentPlanText(500, 1, 500.25)).toContain("1 monthly payment of");
  expect(paymentPlanText(500, 1, 500.25)).not.toContain("1 monthly payments");
  expect(paymentPlanText(500, 23, 500.25)).toContain("23 monthly payments of");
  // Equal amounts collapse to one uniform series rather than naming a
  // "different" final payment that is not different.
  expect(paymentPlanText(500, 23, 500)).toBe("24 monthly payments of $500.00");
});

// --- 4. status semantics -----------------------------------------------------

test("an existing offer is reported as state, not as a dead button", async ({ page }) => {
  await signInAsStaff(page);
  const applicant = fictionalApplicant("Wren", /* even ssn */ true, 100_000);
  await submitApplication(page, applicant);
  const appId = await currentAppId(page);
  await getDecision(page);
  await page.goto(`/underwriting/${appId}`);

  const status = page.getByTestId("offer-exists");
  await expect(status).toBeVisible({ timeout: 15_000 });
  await expect(status).toHaveText("Offer already created");

  // Announced as a status to assistive technology...
  await expect(status).toHaveAttribute("role", "status");
  // ...and NOT presented as an action at all.
  expect(await status.evaluate((el) => el.tagName.toLowerCase())).not.toBe("button");
  await expect(page.getByRole("button", { name: "Offer already created" })).toHaveCount(0);

  // The offer stays read-only: no Make offer control remains to re-create it.
  await expect(page.getByRole("button", { name: "Make offer" })).toHaveCount(0);
});

test("the Make offer control has correct disabled semantics when it is unavailable", async ({ page }) => {
  await signInAsStaff(page);
  const applicant = fictionalApplicant("Kit", /* odd ssn -> denied */ false, 20_000);
  await submitApplication(page, applicant);
  const appId = await currentAppId(page);
  await getDecision(page);
  await page.goto(`/underwriting/${appId}`);

  const button = page.getByRole("button", { name: "Make offer" });
  if (await button.count()) {
    await expect(button).toBeDisabled();
    // aria-disabled as well as the attribute, so the reason is announced
    // rather than the control being skipped silently.
    await expect(button).toHaveAttribute("aria-disabled", "true");
    await expect(button).toHaveAttribute("title", /cannot be created/i);
  }
});

// --- mobile ------------------------------------------------------------------

test("the disclosure does not overflow horizontally on a phone viewport", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 812 });
  await borrowerOffer(page);

  const schedule = page.getByTestId("payment-schedule");
  await expect(schedule).toBeVisible();

  // The page itself must not scroll sideways -- the usual symptom of a fixed
  // width or an unbreakable string in a narrow column.
  const overflows = await page.evaluate(
    () => document.documentElement.scrollWidth > document.documentElement.clientWidth + 1,
  );
  expect(overflows, "the page scrolls horizontally at 375px").toBe(false);

  const box = await page.locator(".tila").boundingBox();
  const inner = await schedule.boundingBox();
  expect(inner!.x + inner!.width).toBeLessThanOrEqual(box!.x + box!.width + 1);
  expect(inner!.y + inner!.height).toBeLessThanOrEqual(box!.y + box!.height + 1);
});

// --- keyboard / screen reader ------------------------------------------------

test("the disclosure remains reachable and correctly structured for assistive tech", async ({ page }) => {
  await borrowerOffer(page);

  // The federal box keeps its heading text, so a screen-reader user can find it.
  await expect(page.getByText(/Federal Truth-in-Lending Disclosure/i)).toBeVisible();

  // Rate labels are associated with their values as text, not conveyed by
  // position or colour alone.
  await expect(page.getByText("Interest rate (note rate)")).toBeVisible();
  await expect(page.getByText("Federal APR")).toBeVisible();

  // The accept control is still keyboard reachable after the layout change.
  const accept = page.getByRole("button", { name: "Accept offer" });
  await accept.focus();
  await expect(accept).toBeFocused();
});
