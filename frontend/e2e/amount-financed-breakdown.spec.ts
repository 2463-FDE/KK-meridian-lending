import { test, expect } from "@playwright/test";
import type { Client } from "pg";
import { dbClient, signInAsStaff } from "./fixtures";

/**
 * The Amount Financed box explains itself, and the four federal boxes stay four.
 *
 * "Amount Financed -- the amount of credit provided to you" is a NET figure. A
 * borrower who applied for $9,000 and reads $8,730 has a $270 gap with nothing
 * on the page accounting for it, because the origination fee is prepaid and
 * never reaches them.
 *
 * Two properties are worth a browser test rather than a unit test:
 *
 *   1. **the three numbers add up on screen.** The fee is derived on the server
 *      by SUBTRACTING the two stored amounts, precisely so this holds. Read off
 *      the rendered page, in the borrower's own units, they must foot;
 *   2. **it is still one of four boxes.** The federal disclosure IS the four
 *      cells; a fifth would change what that disclosure looks like. The
 *      breakdown lives inside the Amount Financed cell.
 *
 * The legacy case gets its own test because it is the one that must NOT invent
 * a figure: a pre-0030 offer stored no principal, and the only recoverable value
 * is `amount_financed / (1 - fee_pct)`, which lands a cent away from what the
 * borrower asked for.
 */

async function withDb<T>(fn: (c: Client) => Promise<T>): Promise<T> {
  const client = dbClient();
  await client.connect();
  try {
    return await fn(client);
  } finally {
    await client.end();
  }
}

/** "$8,730.00" -> 8730 */
function money(text: string | null): number {
  if (!text) throw new Error("no text to read a money figure from");
  const cleaned = text.replace(/[^0-9.]/g, "");
  if (!cleaned) throw new Error(`no digits in ${JSON.stringify(text)}`);
  return Number(cleaned);
}

/** An application whose offer stored a principal, i.e. anything post-0030. */
async function anAppWithAStoredPrincipal(): Promise<number> {
  const row = await withDb(
    async (c) =>
      (
        await c.query(
          `SELECT app_id FROM offers
            WHERE principal IS NOT NULL AND amount_financed IS NOT NULL
            ORDER BY id DESC LIMIT 1`,
        )
      ).rows[0],
  );
  if (!row) throw new Error("no offer in the demo database stores a principal");
  return row.app_id;
}

test("the underwriting box shows a breakdown that foots", async ({ page }) => {
  const appId = await anAppWithAStoredPrincipal();

  await signInAsStaff(page, "underwriter");
  await page.goto(`/underwriting/${appId}`);

  const breakdown = page.getByTestId("amount-financed-breakdown");
  await expect(breakdown).toBeVisible({ timeout: 20_000 });

  const rows = breakdown.locator("div");
  const requested = money(await rows.nth(0).locator("dd").textContent());
  const fee = money(await rows.nth(1).locator("dd").textContent());
  const financed = money(await rows.nth(2).locator("dd").textContent());

  // Read off the page, not from the API. The arithmetic is the server's; this
  // is the check that what a human sees is self-consistent.
  expect(Number((requested - fee).toFixed(2))).toBe(financed);

  // And it agrees with the cell above it, which is the federal figure.
  const cellValue = money(
    await page
      .locator(".tila-cell", { hasText: "Amount Financed" })
      .locator(".tila-cell-value")
      .first()
      .textContent(),
  );
  expect(cellValue).toBe(financed);
});

test("the fee shown is the server's, not the fee percentage re-applied", async ({
  page,
}) => {
  /**
   * The cent. `amount_financed` stores the ROUNDED DIFFERENCE, so for a
   * principal whose fee lands on a half cent the stored fee and
   * `round(principal * 3%)` disagree -- and only one of them makes the box foot.
   * This asserts the page shows the difference between the two STORED figures.
   */
  const appId = await anAppWithAStoredPrincipal();
  const stored = await withDb(
    async (c) =>
      (
        await c.query(
          `SELECT principal::float8 AS principal,
                  amount_financed::float8 AS amount_financed
             FROM offers WHERE app_id = $1 ORDER BY id DESC LIMIT 1`,
          [appId],
        )
      ).rows[0],
  );

  await signInAsStaff(page, "underwriter");
  await page.goto(`/underwriting/${appId}`);

  const breakdown = page.getByTestId("amount-financed-breakdown");
  await expect(breakdown).toBeVisible({ timeout: 20_000 });
  const fee = money(await breakdown.locator("div").nth(1).locator("dd").textContent());

  expect(fee).toBe(
    Number((stored.principal - stored.amount_financed).toFixed(2)),
  );
});

test("the federal disclosure is still four boxes", async ({ page }) => {
  /**
   * The breakdown is inside the Amount Financed cell. A fifth cell would change
   * what the federal disclosure looks like, and the four cells are the
   * disclosure.
   */
  const appId = await anAppWithAStoredPrincipal();

  await signInAsStaff(page, "underwriter");
  await page.goto(`/underwriting/${appId}`);

  await expect(page.getByTestId("amount-financed-breakdown")).toBeVisible({
    timeout: 20_000,
  });
  await expect(page.locator(".tila-grid .tila-cell")).toHaveCount(4);

  // And the four are the four, in the federal order.
  const labels = await page.locator(".tila-grid .tila-cell-label").allTextContents();
  expect(labels).toEqual([
    "Annual Percentage Rate",
    "Finance Charge",
    "Amount Financed",
    "Total of Payments",
  ]);
});

test("a historical offer says the breakdown is unavailable instead of inventing one", async ({
  page,
}) => {
  /**
   * Built by nulling `principal` on a copy of a real offer, then restoring it.
   * That is exactly the pre-0030 shape, and the assertion is about what the page
   * refuses to do: the recoverable value is `amount_financed / (1 - fee_pct)`,
   * a cent away from the real principal, and printing it under "amount
   * requested" would state a figure the borrower was never quoted.
   */
  const appId = await anAppWithAStoredPrincipal();

  // The WHOLE contractual group, read before it is cleared, and restored
  // afterwards. Two things forced this shape and both are worth stating:
  //
  //   * `offers_schedule_all_or_nothing` refuses a row holding some of these
  //     and not others -- nulling `principal` alone raised a check-constraint
  //     violation. That is the database being right: a half-stored contract is
  //     not a state any real offer reaches, so the fixture must build the
  //     genuine pre-0030 shape rather than an impossible one;
  //   * the exact values are saved and put back. An earlier version restored
  //     the principal by inverting `amount_financed` through the fee, which is a
  //     cent away from the real figure -- so the cleanup would have left the demo
  //     offer permanently wrong. That inversion being lossy is precisely what
  //     this test asserts the page will not display; using it to restore data
  //     would have been the same mistake, inside the test for it.
  const saved = await withDb(
    async (c) =>
      (
        await c.query(
          `SELECT id, principal::float8 AS principal,
                  note_rate_pct::float8 AS note_rate_pct,
                  regular_payment_count, final_payment::float8 AS final_payment,
                  term_months, schedule_version
             FROM offers WHERE app_id = $1 ORDER BY id DESC LIMIT 1`,
          [appId],
        )
      ).rows[0],
  );
  await withDb((c) =>
    c.query(
      `UPDATE offers SET principal = NULL, note_rate_pct = NULL,
                         regular_payment_count = NULL, final_payment = NULL,
                         term_months = NULL, schedule_version = NULL
         WHERE id = $1`,
      [saved.id],
    ),
  );

  try {
    await signInAsStaff(page, "underwriter");
    await page.goto(`/underwriting/${appId}`);

    await expect(
      page.getByTestId("amount-financed-breakdown-unavailable"),
    ).toBeVisible({ timeout: 20_000 });
    await expect(
      page.getByText("Amount financed breakdown unavailable for this historical offer."),
    ).toBeVisible();
    await expect(page.getByTestId("amount-financed-breakdown")).toHaveCount(0);

    // The federal figure itself is untouched -- a missing breakdown is not a
    // missing disclosure.
    await expect(
      page.locator(".tila-cell", { hasText: "Amount Financed" }).first(),
    ).toBeVisible();
    // Still four boxes.
    await expect(page.locator(".tila-grid .tila-cell")).toHaveCount(4);
  } finally {
    await withDb((c) =>
      c.query(
        `UPDATE offers SET principal = $2, note_rate_pct = $3,
                           regular_payment_count = $4, final_payment = $5,
                           term_months = $6, schedule_version = $7
           WHERE id = $1`,
        [saved.id, saved.principal, saved.note_rate_pct,
          saved.regular_payment_count, saved.final_payment, saved.term_months,
          saved.schedule_version],
      ),
    );
  }
});
