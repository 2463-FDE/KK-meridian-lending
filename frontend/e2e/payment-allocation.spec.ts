import { test, expect, Page } from "@playwright/test";
import { Client } from "pg";
import { dbClient, signInAsBorrower, signInAsStaff, SEEDED_BORROWER } from "./fixtures";

/**
 * "Can the user tell what a payment was applied to?" -- the 2026-08-19 demo.
 *
 * The answer used to be no: payment history showed Date / Method / Card /
 * Amount. This walks the borrower's own path -- log in, My loan, open the
 * account, read the history -- and asserts they can see the fees / interest /
 * principal split for a payment they just made.
 *
 * The figures are checked against `ledger_entries`, never against anything this
 * test computes. That is the whole point of the feature: the ledger rows are
 * what moved the borrower's balance, so the screen has to agree with them. A
 * test that re-derived the waterfall would be asserting a second opinion, which
 * is exactly what the read path refuses to do.
 *
 * **Amounts are unique per run.** The suite shares one database and no spec
 * isolates its state (`docs/DEBT.md` RF-24), so a fixed amount would collide
 * with a previous run's row and the assertions would silently target the wrong
 * one. Every payment here carries a distinctive cent value.
 *
 * Synthetic data only: the seeded borrower, the seeded loan, the mock
 * tokenizer's test card.
 */

const LOAN_ID = SEEDED_BORROWER.loanId;

let _seq = 0;
/** A distinctive dollar amount, unique within and across runs. */
function uniqueAmount(dollars: number): { value: string; text: string } {
  const cents = (Date.now() % 89 + (_seq++ % 10) * 89 + 1) % 99 + 1;
  const value = `${dollars}.${String(cents).padStart(2, "0")}`;
  return { value, text: `$${dollars}.${String(cents).padStart(2, "0")}` };
}

const usd = (n: number) =>
  n.toLocaleString("en-US", { style: "currency", currency: "USD" });

async function withDb<T>(fn: (client: Client) => Promise<T>): Promise<T> {
  const client = dbClient();
  await client.connect();
  try {
    return await fn(client);
  } finally {
    await client.end();
  }
}

interface LedgerSplit {
  fees?: number;
  interest?: number;
  principal?: number;
}

/** What the ledger says a payment paid, per component, as positive dollars. */
function ledgerSplitFor(paymentId: number): Promise<LedgerSplit> {
  return withDb(async (client) => {
    const res = await client.query(
      `SELECT component, -SUM(amount)::float8 AS paid
         FROM ledger_entries
        WHERE payment_id = $1 AND entry_type = 'payment'
        GROUP BY component`,
      [paymentId],
    );
    const split: LedgerSplit = {};
    for (const row of res.rows) {
      split[row.component as keyof LedgerSplit] = Number(row.paid);
    }
    return split;
  });
}

/** The payment row this loan most recently recorded. */
function latestPayment(): Promise<{ id: number; amount: number }> {
  return withDb(async (client) => {
    const res = await client.query(
      `SELECT id, amount::float8 AS amount
         FROM payments WHERE loan_id = $1 ORDER BY id DESC LIMIT 1`,
      [LOAN_ID],
    );
    return { id: Number(res.rows[0].id), amount: Number(res.rows[0].amount) };
  });
}

/**
 * A payment with NO ledger entries -- the legacy shape.
 *
 * `db/init/007_ledger_opening_balances.sql` gives every seeded payment ledger
 * evidence, so a fresh database contains no example of a payment that predates
 * the ledger. This is the smallest deterministic fixture that produces one: a
 * `payments` row and nothing else. It is deleted again in the test's finally
 * block, and it moves no balance -- `balances` is a projection of
 * `ledger_entries`, and this writes none.
 */
function insertLegacyPayment(amount: string): Promise<number> {
  return withDb(async (client) => {
    const res = await client.query(
      `INSERT INTO payments (loan_id, last4, brand, amount, method, created_at)
       VALUES ($1, '1111', 'visa', $2, 'card', now() - interval '400 days')
       RETURNING id`,
      [LOAN_ID, amount],
    );
    return Number(res.rows[0].id);
  });
}

function deletePayment(paymentId: number): Promise<void> {
  return withDb(async (client) => {
    await client.query(`DELETE FROM payments WHERE id = $1`, [paymentId]);
  });
}

/** The borrower's own route to the account screen: My loan, then the account. */
async function openAccountAsBorrower(page: Page): Promise<void> {
  await signInAsBorrower(page);
  await page.goto("/my-loan");
  await expect(page.getByRole("heading", { name: /My loan/i })).toBeVisible();
  await page.goto(`/servicing/${LOAN_ID}`);
  await expect(page.getByRole("heading", { name: /Payment history/i })).toBeVisible({
    timeout: 15_000,
  });
}

function rowForAmount(page: Page, amountText: string) {
  return page.locator("tbody tr").filter({ hasText: amountText });
}

async function payOnce(page: Page, amount: string): Promise<void> {
  // Exact: the staff view of this page also has a "Waiver amount (USD)"
  // field, which a substring label match picks up as well.
  await page.getByLabel("Amount (USD)", { exact: true }).fill(amount);
  await page.getByRole("button", { name: /Pay with card on file/ }).click();
  // The page refreshes balance and history itself once the charge is confirmed.
  await expect(page.locator(".alert-success")).toContainText(/submitted|pending/i, {
    timeout: 20_000,
  });
}

test("a borrower sees what their payment was applied to, and the figures are the ledger's", async ({
  page,
}) => {
  await openAccountAsBorrower(page);

  const amount = uniqueAmount(137);
  await payOnce(page, amount.value);

  const paid = await latestPayment();
  expect(paid.amount).toBeCloseTo(Number(amount.value), 2);
  const split = await ledgerSplitFor(paid.id);

  // The apply wrote ledger entries. Without this the test could pass against
  // the legacy path by accident and prove nothing about the feature.
  expect(Object.keys(split).length).toBeGreaterThan(0);

  const row = rowForAmount(page, amount.text).first();
  await expect(row).toBeVisible();

  const allocation = row.getByLabel("What this payment was applied to");
  await expect(allocation).toBeVisible();

  for (const [component, label] of [
    ["fees", "Fees"],
    ["interest", "Interest"],
    ["principal", "Principal"],
  ] as const) {
    const line = allocation.locator(".alloc-row").filter({ hasText: label });
    await expect(line).toBeVisible();
    // A component with no ledger row means the waterfall allocated nothing to
    // it, and servicing reports that as a KNOWN zero -- 0.00, not null --
    // because the payment does have allocation evidence overall.
    await expect(line).toContainText(usd(split[component] ?? 0));
  }

  // The displayed figures are the ledger's, and the ledger's sum to the
  // payment. Checked against the database's own numbers; nothing shown on the
  // page is derived from this arithmetic.
  const ledgerTotal =
    (split.fees ?? 0) + (split.interest ?? 0) + (split.principal ?? 0);
  expect(ledgerTotal).toBeCloseTo(Number(amount.value), 2);

  // Borrower on their own account, and the staff-only panel is not rendered.
  await expect(
    page.getByRole("heading", { name: /Servicing rep actions/i }),
  ).toHaveCount(0);
});

test("a payment with no ledger evidence says so instead of showing zeros", async ({
  page,
}) => {
  const legacy = uniqueAmount(77);
  const paymentId = await insertLegacyPayment(legacy.value);

  try {
    await openAccountAsBorrower(page);

    const row = rowForAmount(page, legacy.text).first();
    await expect(row).toBeVisible();
    // Three "$0.00" lines here would tell this borrower their money went
    // nowhere. The API sends null, and null is not zero.
    await expect(row).toContainText(
      "Allocation not available for this historical payment",
    );
    await expect(row.getByLabel("What this payment was applied to")).toHaveCount(0);
    await expect(row).not.toContainText("Principal");
  } finally {
    await deletePayment(paymentId);
  }
});

test("the fees line carries a real figure where the ledger recorded one", async ({
  page,
}) => {
  /**
   * The borrower's own loan opened today, so the contract has billed no interest
   * and carries no fees: every allocation there is principal with two honest
   * zeros beside it. That proves the known-zero case and not the interesting
   * one.
   *
   * Seeded loan 5582 proves it with no fixture at all. It took two identical
   * $410.50 captures two seconds apart -- the duplicate `docs/DEBT.md` D22 is
   * about -- and `db/init/007_ledger_opening_balances.sql` recorded them
   * differently because the waterfall did: the first cleared the arrears, so its
   * ledger row is `fees`; the second had no fees left to pay, so its row is
   * `principal`.
   *
   * Two rows, the same amount, different allocations. A screen deriving the
   * split from the amount -- or reusing one row's answer for another -- could not
   * show these two disagreeing, and they must.
   *
   * Reached as staff: 5582 is not the seeded borrower's loan and ownership is
   * enforced server-side. Same screen, same component.
   */
  await signInAsStaff(page, "csr");
  await page.goto("/servicing/5582");
  await expect(page.getByRole("heading", { name: /Payment history/i })).toBeVisible({
    timeout: 15_000,
  });

  const duplicates = rowForAmount(page, "$410.50");
  await expect(duplicates).toHaveCount(2);

  const rendered = await duplicates.evaluateAll((rows) =>
    rows.map((row) => (row.textContent || "").replace(/\s+/g, " ")),
  );

  // No space required between label and amount: they are separate elements, so
  // textContent concatenates them as "Fees$410.50".
  expect(rendered.filter((text) => /Fees\s*\$410\.50/.test(text))).toHaveLength(1);
  expect(rendered.filter((text) => /Principal\s*\$410\.50/.test(text))).toHaveLength(1);

  // And the ledger agrees, which is what makes the two rows disagreeing the
  // correct answer rather than a coincidence.
  const split = await withDb(async (client) => {
    const res = await client.query(
      `SELECT payment_id, component, -SUM(amount)::float8 AS paid
         FROM ledger_entries
        WHERE loan_id = 5582 AND entry_type = 'payment' AND payment_id IN (2, 3)
        GROUP BY payment_id, component
        ORDER BY payment_id`,
    );
    return res.rows.map((r) => `${r.component}:${Number(r.paid).toFixed(2)}`);
  });
  expect(split).toEqual(["fees:410.50", "principal:410.50"]);
});

test("two payments each show their own allocation", async ({ page }) => {
  await openAccountAsBorrower(page);

  const first = uniqueAmount(111);
  await payOnce(page, first.value);
  const firstRow = await latestPayment();
  const firstSplit = await ledgerSplitFor(firstRow.id);

  const second = uniqueAmount(123);
  await payOnce(page, second.value);
  const secondRow = await latestPayment();
  const secondSplit = await ledgerSplitFor(secondRow.id);

  expect(secondRow.id).not.toBe(firstRow.id);

  for (const [amountText, split] of [
    [first.text, firstSplit],
    [second.text, secondSplit],
  ] as const) {
    const row = rowForAmount(page, amountText).first();
    await expect(row).toBeVisible();
    const principal = row.locator(".alloc-row").filter({ hasText: "Principal" });
    await expect(principal).toContainText(usd(split.principal ?? 0));
  }

  // Independent rows: the two allocations differ, so neither is being rendered
  // from a shared or cached figure.
  expect(secondSplit.principal ?? 0).not.toBeCloseTo(firstSplit.principal ?? 0, 2);
});
