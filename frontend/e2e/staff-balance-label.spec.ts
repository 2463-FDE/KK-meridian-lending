import { test, expect } from "@playwright/test";
import type { Page } from "@playwright/test";
import type { Client } from "pg";
import { dbClient, signInAsStaff } from "./fixtures";

/**
 * The staff servicing page's top card names the balance it is actually showing.
 *
 * `balances.balance` is projected ONLY from `component = 'principal'` ledger
 * entries (db/migrations/0035). Fees land in `past_due` -- the card immediately
 * beside it -- and interest projects nowhere. The top card was labelled "Current
 * balance", which reads as everything owed. It is not: an operator reading it
 * that way understates what a borrower owes by the entire fee balance sitting in
 * the next card along.
 *
 * The same page already said "Current principal balance" twice further down, over
 * the same figure, so the card was contradicting its own page. The borrower view
 * was corrected in #87; this is the staff view, which kept the old wording.
 *
 * **Anchored on `data-testid`, deliberately.** A `getByText("Current principal
 * balance")` would match three places on this page now, and Playwright's strict
 * mode would fail on the ambiguity -- or worse, a loose matcher would pass
 * because one of the OTHER two labels was present while the card itself still
 * said the wrong thing. The testid names this card and nothing else.
 *
 * **The value is asserted too, not just the label.** A label is only correct if
 * it sits over the number it claims to describe. Renaming the card while the
 * value came from `past_due` would satisfy a label-only test and be a worse bug
 * than the one being fixed, so the test picks a loan whose two balances DIFFER
 * and checks both cards against the database.
 *
 * One page load, all assertions grouped: per `adjustment-preview.spec.ts`, a
 * test-per-assertion pattern here trips the gateway's rate limit
 * (`RATE_LIMIT_MAX_REQUESTS`, 120/60s) and the resulting `/auth/me` failures look
 * exactly like flakiness.
 */

// Sign-in, navigation and the `/auth/me` round trip precede the first assertion,
// and this file also pays the Next.js cold page compile.
test.describe.configure({ timeout: 90_000 });

async function withDb<T>(fn: (c: Client) => Promise<T>): Promise<T> {
  const client = dbClient();
  await client.connect();
  try {
    return await fn(client);
  } finally {
    await client.end();
  }
}

/** "$10,000.00" -> 10000 */
function money(text: string | null): number {
  if (!text) throw new Error("no text to read a money figure from");
  const cleaned = text.replace(/[^0-9.]/g, "");
  if (!cleaned) throw new Error(`no digits in ${JSON.stringify(text)}`);
  return Number(cleaned);
}

/**
 * A serviced loan whose principal and past-due balances DIFFER.
 *
 * The difference is what makes a mis-paired label detectable rather than
 * accidentally right: if the card ever renders `past_due` under a principal
 * label, the numbers no longer agree and this test says so.
 */
async function aLoanWithDistinctBalances(): Promise<{
  loanId: number;
  balance: number;
  pastDue: number;
}> {
  const row = await withDb(
    async (c) =>
      (
        await c.query(
          `SELECT b.loan_id, b.balance::float8 AS balance,
                  COALESCE(b.past_due, 0)::float8 AS past_due
             FROM balances b JOIN loans l ON l.id = b.loan_id
            WHERE l.status = 'current'
              AND b.balance <> COALESCE(b.past_due, 0)
            ORDER BY b.loan_id LIMIT 1`,
        )
      ).rows[0],
  );
  if (!row) {
    throw new Error(
      "no serviced loan has a principal balance distinct from its past-due " +
        "balance, so a mis-paired balance card would be undetectable",
    );
  }
  return { loanId: row.loan_id, balance: row.balance, pastDue: row.past_due };
}

async function openStaffLoanPage(page: Page, loanId: number): Promise<void> {
  await page.goto(`/servicing/${loanId}`);
  // The top cards render before the role resolves, but waiting for the staff
  // heading keeps this consistent with the other staff specs and ensures the
  // page finished deciding what to show.
  await expect(
    page.getByRole("heading", { name: "Servicing rep actions" }),
  ).toBeVisible({ timeout: 60_000 });
}

test("the staff balance card names the balance it shows, and shows it", async ({
  page,
}) => {
  const loan = await aLoanWithDistinctBalances();

  await signInAsStaff(page, "csr");
  await openStaffLoanPage(page, loan.loanId);

  // 1. The label, read off this card alone.
  await expect(page.getByTestId("kpi-principal-label")).toHaveText(
    /^\s*Current principal balance\s*$/,
  );

  // 2. The value under it is the principal projection, not the fee one.
  expect(
    money(await page.getByTestId("kpi-principal-value").textContent()),
  ).toBeCloseTo(loan.balance, 2);

  // 3. The card beside it still holds the fees, so the two were not swapped.
  //    Scoped to that card rather than searched for on the page.
  const pastDueCard = page.locator(".kpi", { hasText: "Past due" });
  expect(money(await pastDueCard.locator(".kpi-value").textContent())).toBeCloseTo(
    loan.pastDue,
    2,
  );

  // 4. No card anywhere on this page still says the bare, ambiguous thing.
  //    Checked across every `.kpi-label` rather than by page text: the precise
  //    phrase now appears three times on this page, so a text search proves
  //    nothing about which occurrence it found.
  const labels = await page.locator(".kpi-label").allTextContents();
  expect(labels.length).toBeGreaterThan(0);
  for (const label of labels) {
    expect(label.trim()).not.toBe("Current balance");
  }
});
