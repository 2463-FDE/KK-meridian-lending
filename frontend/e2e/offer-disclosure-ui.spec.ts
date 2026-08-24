import { test, expect } from "@playwright/test";
import {
  fictionalApplicant,
  submitApplication,
  currentAppId,
  getDecision,
  dbClient,
  signInAsStaff,
} from "./fixtures";
// Static import, not a dynamic await import(). Playwright transpiles the spec's
// own imports; a runtime import() of a .ts module resolved to raw TypeScript in
// CI and failed with "SyntaxError: Unexpected token 'export'" -- it happened to
// work locally, which is exactly the kind of difference CI exists to catch.
import { paymentPlanText } from "../lib/format";

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

test("the staff offer prompt names the server's note rate, never an APR", async ({
  page,
  request,
}) => {
  /**
   * The figure comes from `GET /los/pricing` now, not from a constant this
   * screen owned (PR #80). So the assertion reads the published rate and looks
   * for that, rather than for a hardcoded 7.99 -- a test pinned to the number
   * would fail a demo run at a different configured rate while proving nothing
   * about where the number came from.
   */
  const pricing = await (await request.get("http://localhost:8000/los/pricing")).json();
  const published = `${Number(pricing.note_rate_pct).toFixed(2)}%`;

  await signInAsStaff(page);
  const applicant = fictionalApplicant("Teo", /* even ssn */ true, 100_000);
  await submitApplication(page, applicant);
  const appId = await currentAppId(page);
  await getDecision(page);

  await page.goto(`/underwriting/${appId}`);
  const prompt = page.getByText(/Generate a Truth-in-Lending offer/);
  await expect(prompt).toBeVisible({ timeout: 15_000 });

  await expect(prompt).toContainText(`at a ${published} note rate`);
  // And it says whose figure it is, so a staff member does not read it as
  // something this screen decided.
  await expect(prompt).toContainText(/set by the server/i);

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
  // The TEXT, not its wrapper. Measuring the wrapper proved nothing: it is an
  // ordinary in-flow child, so its border box is contained by `.tila` whether
  // or not it has any padding -- deleting `.tila-schedule`'s padding entirely
  // left this test green while the text went back to sitting on the disclosure
  // border, which is the reported defect. Review finding on PR #10.
  const text = schedule.locator(".tila-schedule-value");
  const outer = await box.boundingBox();
  const inner = await text.boundingBox();
  expect(outer).not.toBeNull();
  expect(inner).not.toBeNull();

  // Containment first: the text must be inside the box on every edge.
  expect(inner!.y).toBeGreaterThanOrEqual(outer!.y - 1);
  expect(inner!.x).toBeGreaterThanOrEqual(outer!.x - 1);
  expect(inner!.y + inner!.height).toBeLessThanOrEqual(outer!.y + outer!.height + 1);
  expect(inner!.x + inner!.width).toBeLessThanOrEqual(outer!.x + outer!.width + 1);

  // Then the inset itself, which is the thing that regresses. A real gap on
  // every side, measured from rendered geometry rather than read back from the
  // stylesheet -- a rule that is overridden further down the cascade would
  // still read as present.
  const MIN_INSET = 8;
  expect(inner!.x - outer!.x, "left inset").toBeGreaterThanOrEqual(MIN_INSET);
  expect(outer!.x + outer!.width - (inner!.x + inner!.width), "right inset")
    .toBeGreaterThanOrEqual(MIN_INSET);
  expect(outer!.y + outer!.height - (inner!.y + inner!.height), "bottom inset")
    .toBeGreaterThanOrEqual(MIN_INSET);

  // And the divider above it: the schedule sits under a 2px rule, so the text
  // must clear that too rather than resting on it.
  const dividerGap = await schedule.evaluate((el) => {
    const value = el.querySelector(".tila-schedule-value") as HTMLElement | null;
    if (!value) return -1;
    return value.getBoundingClientRect().top - el.getBoundingClientRect().top;
  });
  expect(dividerGap, "gap between the divider and the schedule text")
    .toBeGreaterThanOrEqual(MIN_INSET);
});

test("a singular regular payment reads '1 monthly payment', not '1 payments'", () => {
  // Pure formatter behaviour, so it needs no browser: the plural rule is the
  // thing under test, and reaching a one-period loan through the UI would test
  // the wizard instead.
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

test("a legacy offer that cannot board offers a way to regenerate it", async ({ page }) => {
  /* An unaccepted pre-0030 offer has its five TILA amounts and no stored
   * schedule, so Accept is disabled and its tooltip tells staff to regenerate.
   * There was nothing to click: this branch rendered the same static "Offer
   * already created" label as a complete offer, so the audited repair path
   * added for exactly these rows had no caller in any production UI. Review
   * finding on PR #10.
   *
   * The legacy shape is produced by clearing the schedule columns directly --
   * the wizard cannot create one, because every offer written since 0030 has
   * them.
   */
  await signInAsStaff(page);
  const applicant = fictionalApplicant("Marlowe", /* even ssn */ true, 100_000);
  await submitApplication(page, applicant);
  const appId = await currentAppId(page);
  await getDecision(page);

  const client = dbClient();
  await client.connect();
  try {
    await client.query(
      "UPDATE offers SET regular_payment_count = NULL, final_payment = NULL, " +
      "term_months = NULL, schedule_version = NULL, note_rate_pct = NULL, " +
      "principal = NULL WHERE app_id = $1",
      [appId],
    );
  } finally {
    await client.end();
  }

  await page.goto(`/underwriting/${appId}`);

  // The static label is gone, and a real control is in its place.
  await expect(page.getByTestId("offer-exists")).toHaveCount(0);
  const regenerate = page.getByTestId("regenerate-offer");
  await expect(regenerate).toBeVisible({ timeout: 15_000 });
  await expect(regenerate).toBeEnabled();

  await regenerate.click();

  // Regeneration writes the stored schedule, so the offer becomes boardable --
  // which is the whole point of having the control.
  await expect(page.getByTestId("offer-exists")).toBeVisible({ timeout: 20_000 });

  const check = dbClient();
  await check.connect();
  try {
    const row = await check.query(
      "SELECT final_payment, term_months, schedule_version, note_rate_pct, principal " +
      "FROM offers WHERE app_id = $1",
      [appId],
    );
    expect(row.rowCount).toBe(1);
    for (const [column, value] of Object.entries(row.rows[0])) {
      expect(value, `${column} is still null after regenerating`).not.toBeNull();
    }
    // And it is audited: a regenerated disclosure is a new disclosure.
    const audit = await check.query(
      "SELECT 1 FROM audit_logs WHERE action = 'offer.incomplete_terms_repaired' " +
      "AND detail LIKE $1",
      [`%app_id=${appId}%`],
    );
    expect(audit.rowCount).toBe(1);
  } finally {
    await check.end();
  }
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

  // Same blind spot as the desktop assertion had: measure the text.
  const box = await page.locator(".tila").boundingBox();
  const inner = await schedule.locator(".tila-schedule-value").boundingBox();
  expect(inner!.x + inner!.width).toBeLessThanOrEqual(box!.x + box!.width + 1);
  expect(inner!.y + inner!.height).toBeLessThanOrEqual(box!.y + box!.height + 1);

  // The INSET regression is asserted at desktop width, in the test above: at
  // 375px `.tila` contributes horizontal padding of its own, so a gap measured
  // here survives `.tila-schedule`'s padding being deleted and would be an
  // assertion that cannot fail. Verified by deleting that padding and watching
  // only the desktop test go red. What this viewport is for is overflow and
  // containment, both of which are now measured on the TEXT rather than on its
  // wrapper -- the blind spot the wrapper-only version had.
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
