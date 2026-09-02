import { test, expect, Page } from "@playwright/test";
import { Client } from "pg";
import { dbClient, signInAsBorrower, SEEDED_BORROWER } from "./fixtures";

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
      // `applied_at` is set deliberately. `payments.auth_status` defaults to
      // 'captured' and payment-service's reconciler drains
      // `auth_status = 'captured' AND applied_at IS NULL` -- so a row without it
      // is work for a background loop, which would write the very ledger entries
      // this fixture exists to lack, and move the borrower's balance while the
      // test watched. Recorded-as-applied with no ledger evidence is exactly the
      // pre-ledger shape anyway.
      `INSERT INTO payments (loan_id, last4, brand, amount, method, created_at,
                             auth_status, applied_at)
       VALUES ($1, '1111', 'visa', $2, 'card', now() - interval '400 days',
               'captured', now() - interval '400 days')
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
  // Scoped to the payment-history table by name.
  //
  // This read `page.locator("tbody tr")` across the whole page, which was
  // unambiguous while payment history was the only table on it. The account
  // activity panel added a second one ABOVE it, and a payment appears in both --
  // so `.first()` began matching the activity row, which carries no allocation
  // label, and this spec failed on a page that was rendering correctly.
  //
  // Scoped rather than reordered or loosened: what this test is about is what
  // PAYMENT HISTORY shows, and naming that table says so.
  return page
    .getByTestId("payment-history")
    .locator("tbody tr")
    .filter({ hasText: amountText });
}

async function payOnce(page: Page, amount: string): Promise<void> {
  // Exact: the staff view of this page also has a "Waiver amount (USD)"
  // field, which a substring label match picks up as well.
  await page.getByLabel("Amount (USD)", { exact: true }).fill(amount);
  await page.getByRole("button", { name: /Pay with card on file/ }).click();
  // Wait for the OUTCOME the page now reports, whichever of the three it is.
  //
  // This waited for `.alert-success` to say "submitted" or "pending" -- the copy
  // before the payment states were told apart. A captured payment now renders a
  // receipt ("Payment posted"), and a decline renders an error rather than a
  // success alert at all, so waiting on the old wording timed out on a page that
  // was working. Keyed on the outcome test ids instead of on prose.
  await expect(
    page
      .getByTestId("payment-posted")
      .or(page.getByTestId("payment-pending"))
      .or(page.getByTestId("payment-declined")),
  ).toBeVisible({ timeout: 20_000 });
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

test("a captured payment not yet applied says so, rather than 'not available'", async ({
  page,
}) => {
  /**
   * The client's decision: a payment captured but not yet applied reads
   * "Captured -- allocation pending", the same words the receipt uses. Never an
   * estimate, and never the historical-payment wording -- a borrower whose card
   * was charged seconds ago is not reading about a legacy row.
   *
   * Route-mocked rather than seeded, and that is not convenience. A real
   * `auth_status = 'captured' AND applied_at IS NULL` row is precisely what
   * payment-service's reconciler drains: it would apply the payment mid-test and
   * write the ledger entries this state is defined by NOT having. The API side
   * of this contract is covered against real PostgreSQL in
   * `servicing-service/tests/test_history_says_why_an_allocation_is_absent.py`;
   * what only a browser can show is which sentence the borrower reads.
   */
  await page.route(`**/lss/loans/${LOAN_ID}/payments`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        loan_id: LOAN_ID,
        items: [
          {
            id: 999001,
            amount: 150.0,
            method: "card",
            masked_pan: "•••• 1111",
            created_at: new Date().toISOString(),
            // No ledger evidence yet -- that is the state, not a gap.
            applied_to_fees: null,
            applied_to_interest: null,
            applied_to_principal: null,
            auth_status: "captured",
            applied: false,
          },
        ],
      }),
    }),
  );

  await openAccountAsBorrower(page);

  const row = rowForAmount(page, "$150.00").first();
  await expect(row).toBeVisible();
  await expect(row.getByTestId("alloc-pending")).toHaveText(
    "Captured — allocation pending.",
  );
  // Not the legacy wording, and not an allocation table.
  await expect(row).not.toContainText("not available for this historical payment");
  await expect(row.getByLabel("What this payment was applied to")).toHaveCount(0);
  // And no estimate stood in for the missing figures.
  await expect(row).not.toContainText("$0.00");
});


test("a payment awaiting authorization is not described as captured", async ({
  page,
}) => {
  /**
   * Codex ALLOC-PENDING-AUTH-001. `auth_status = 'pending'` fell through to the
   * historical-payment wording, so a borrower whose authorization was in flight
   * -- or left in flight by a crash mid-authorization -- read legacy-gap copy
   * about a payment that is neither historical nor settled.
   *
   * The wording matters more here than in any of the other absence states.
   * `payment-service` inserts the row as `pending` BEFORE it calls the
   * processor, so this borrower's card may never have been charged. "Captured
   * -- allocation pending" would assert a charge this system cannot claim; the
   * historical sentence would call an in-flight payment old. Both are wrong in
   * the borrower's favour in one direction and against it in the other, which
   * is why this asserts the sentence rather than only the state.
   *
   * Route-mocked for the same reason the captured case above is: a real pending
   * row is what the authorization path is actively resolving, so seeding one and
   * then reading the screen races the thing under test. The API side is covered
   * against real PostgreSQL in
   * `servicing-service/tests/test_history_says_why_an_allocation_is_absent.py`.
   */
  await page.route(`**/lss/loans/${LOAN_ID}/payments`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        loan_id: LOAN_ID,
        items: [
          {
            id: 999003,
            amount: 310.0,
            method: "card",
            masked_pan: "•••• 1111",
            created_at: new Date().toISOString(),
            applied_to_fees: null,
            applied_to_interest: null,
            applied_to_principal: null,
            auth_status: "pending",
            applied: false,
          },
        ],
      }),
    }),
  );

  await openAccountAsBorrower(page);

  const row = rowForAmount(page, "$310.00").first();
  await expect(row).toBeVisible();
  await expect(row.getByTestId("alloc-authorizing")).toHaveText(
    "Authorization in progress — nothing applied yet.",
  );
  // Not the captured sentence: nothing has been captured.
  await expect(row).not.toContainText("Captured");
  // Not the legacy wording either.
  await expect(row).not.toContainText("not available for this historical payment");
  // No allocation table, and no zero standing in for an unknown.
  await expect(row.getByLabel("What this payment was applied to")).toHaveCount(0);
  await expect(row).not.toContainText("$0.00");
});


test("a declined payment reads as declined, not as a missing figure", async ({
  page,
}) => {
  /** Nothing was applied. Leaving it in the "not available" bucket invited the
   *  reading that an allocation exists and merely failed to load. */
  await page.route(`**/lss/loans/${LOAN_ID}/payments`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        loan_id: LOAN_ID,
        items: [
          {
            id: 999002,
            amount: 60.0,
            method: "card",
            masked_pan: null,
            created_at: new Date().toISOString(),
            applied_to_fees: null,
            applied_to_interest: null,
            applied_to_principal: null,
            auth_status: "failed",
            applied: false,
          },
        ],
      }),
    }),
  );

  await openAccountAsBorrower(page);

  const row = rowForAmount(page, "$60.00").first();
  await expect(row).toBeVisible();
  await expect(row.getByTestId("alloc-declined")).toHaveText(
    "Declined — nothing applied.",
  );
  await expect(row).not.toContainText("$0.00");
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
