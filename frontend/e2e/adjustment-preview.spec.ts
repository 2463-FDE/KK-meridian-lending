import { test, expect } from "@playwright/test";
import type { Client } from "pg";
import { dbClient, signInAsStaff } from "./fixtures";

/**
 * A staff member proposing a balance adjustment can see which direction they
 * have chosen, and against which balance.
 *
 * The backend semantics are a SIGNED DELTA and stay that way: +450 means the
 * borrower owes $450 more, -450 means $450 less. `new_balance` was removed
 * deliberately in #35 (spec 0002 REQ-VAL-1) -- "set the balance to X" invites a
 * caller to overwrite a figure it read some time ago. What was missing was not
 * the semantics but any way to read them off the screen: an operator typed a
 * number into a field labelled "Change (USD)" with no starting balance beside it
 * and no statement of where it would land.
 *
 * Two things are asserted here that a unit test cannot reach:
 *
 *   1. **the preview is component-aware.** A fees adjustment must be previewed
 *      against past-due fees, not against principal. `balances.balance` is
 *      projected from `component = 'principal'` entries and `past_due` from
 *      `'fees'` (db/migrations/0035), so previewing the wrong one shows a
 *      starting number unrelated to what is being changed;
 *   2. **submitting still moves nothing**, and says so. The preview is the most
 *      likely thing on the page to be misread as an action.
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
 * The difference is the whole requirement: it is what makes previewing against
 * the wrong component detectable instead of accidentally right. No `past_due > 0`
 * condition -- the first version had one and found nothing, because no loan in
 * the seeded portfolio carries arrears. A past-due of 0.00 beside a five-figure
 * principal is if anything a sharper test: a fees preview that reached for the
 * principal balance would show thousands where it should show nothing.
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
        "balance, so a mis-paired preview would be undetectable",
    );
  }
  return { loanId: row.loan_id, balance: row.balance, pastDue: row.past_due };
}

test("the preview shows the principal balance and where a +450 lands", async ({
  page,
}) => {
  const loan = await aLoanWithDistinctBalances();

  await signInAsStaff(page, "csr");
  await page.goto(`/servicing/${loan.loanId}`);

  const preview = page.getByTestId("adjust-preview");
  await expect(preview).toBeVisible({ timeout: 20_000 });

  const rows = preview.locator(".dl-row");
  await expect(rows.nth(0).locator("dt")).toHaveText(/Current principal balance/i);
  expect(money(await rows.nth(0).locator("dd").textContent())).toBeCloseTo(
    loan.balance,
    2,
  );

  await page.locator("#adjust-amount").fill("450");

  // The direction, in words the operator does not have to infer.
  await expect(rows.nth(1).locator("dd")).toHaveText(/\+/);
  expect(money(await rows.nth(2).locator("dd").textContent())).toBeCloseTo(
    loan.balance + 450,
    2,
  );
});

test("a negative change previews downward", async ({ page }) => {
  const loan = await aLoanWithDistinctBalances();

  await signInAsStaff(page, "csr");
  await page.goto(`/servicing/${loan.loanId}`);
  await expect(page.getByTestId("adjust-preview")).toBeVisible({ timeout: 20_000 });

  await page.locator("#adjust-amount").fill("-450");

  const rows = page.getByTestId("adjust-preview").locator(".dl-row");
  // A minus sign, not a plus. The two are one character apart on screen and
  // hundreds of dollars apart in effect.
  await expect(rows.nth(1).locator("dd")).not.toHaveText(/\+/);
  expect(money(await rows.nth(2).locator("dd").textContent())).toBeCloseTo(
    loan.balance - 450,
    2,
  );
});

test("a fees adjustment previews against past-due fees, not principal", async ({
  page,
}) => {
  /**
   * The mis-pairing this test exists for. The loan is chosen so its principal
   * and past-due balances differ, which is what makes previewing the wrong one
   * detectable instead of accidentally correct.
   */
  const loan = await aLoanWithDistinctBalances();
  expect(loan.pastDue).not.toBeCloseTo(loan.balance, 2);

  await signInAsStaff(page, "csr");
  await page.goto(`/servicing/${loan.loanId}`);
  await expect(page.getByTestId("adjust-preview")).toBeVisible({ timeout: 20_000 });

  await page.locator("#adjust-component").selectOption("fees");
  await page.locator("#adjust-amount").fill("10");

  const rows = page.getByTestId("adjust-preview").locator(".dl-row");
  await expect(rows.nth(0).locator("dt")).toHaveText(/Past-due fees now/i);
  expect(money(await rows.nth(0).locator("dd").textContent())).toBeCloseTo(
    loan.pastDue,
    2,
  );
  expect(money(await rows.nth(2).locator("dd").textContent())).toBeCloseTo(
    loan.pastDue + 10,
    2,
  );
});

test("an empty or half-typed amount reads as no change, not as NaN", async ({
  page,
}) => {
  const loan = await aLoanWithDistinctBalances();

  await signInAsStaff(page, "csr");
  await page.goto(`/servicing/${loan.loanId}`);
  const rows = page.getByTestId("adjust-preview").locator(".dl-row");
  await expect(rows.first()).toBeVisible({ timeout: 20_000 });

  // Nothing typed yet.
  await expect(rows.nth(2).locator("dd")).toHaveText("—");

  // Typed rather than filled, and rather than `fill("-")`. This is an
  // `<input type="number">`: the browser refuses "-" as a value, so Playwright's
  // `fill` fails the element's own validation and the test failed on the harness
  // instead of on the page. A person types the minus first all the same, and the
  // control reports an empty value while the number is incomplete -- which is
  // exactly the `parseFloat("") -> NaN` the guard is for.
  await page.locator("#adjust-amount").pressSequentially("-");
  let body = (await page.locator("body").textContent()) ?? "";
  expect(body).not.toContain("NaN");

  // And a decimal point mid-number, the other partial state.
  await page.locator("#adjust-amount").pressSequentially("0.");
  body = (await page.locator("body").textContent()) ?? "";
  expect(body).not.toContain("NaN");
});

test("the preview says it is a preview and that nothing has moved", async ({
  page,
}) => {
  const loan = await aLoanWithDistinctBalances();

  await signInAsStaff(page, "csr");
  await page.goto(`/servicing/${loan.loanId}`);
  await expect(page.getByTestId("adjust-preview")).toBeVisible({ timeout: 20_000 });

  await expect(
    page.getByText(/No money moves when this proposal is submitted/i),
  ).toBeVisible();
  await expect(
    page.getByText(/revalidated when a different authorized staff member approves/i),
  ).toBeVisible();
});

test("submitting the proposal reports that no money moved", async ({ page }) => {
  /**
   * The UI contract only. The preview is the most likely thing on this page to be
   * misread as an action, so what the operator is told afterwards matters.
   *
   * **Deliberately not asserted here: the database state.** An earlier version
   * proposed a real +450, checked that the balance and ledger were untouched, and
   * then tried to clean up after itself. `pending_movements_are_retained()`
   * refused the delete -- a proposal stays on record whatever its outcome, which
   * is the control, not an obstacle. So this spec would have added one
   * undeletable unresolved proposal to a shared demo queue on every run, into a
   * list real people read.
   *
   * That property belongs where the schema is disposable, and it is already
   * proven there:
   * `servicing-service/tests/test_maker_checker_api.py::test_adjust_balance_raises_a_proposal_and_moves_nothing`
   * asserts the proposal is recorded and the balance layer is never called, with
   * a fixture that explodes if anything moves money.
   */
  const loan = await aLoanWithDistinctBalances();

  await signInAsStaff(page, "csr");
  await page.goto(`/servicing/${loan.loanId}`);
  await expect(page.getByTestId("adjust-preview")).toBeVisible({ timeout: 20_000 });

  await page.locator("#adjust-amount").fill("450");
  await page.locator("#adjust-reason").fill("e2e preview check");

  // The button, and there are two of them: the other belongs to the fee waiver.
  const submit = page
    .locator(".card", { hasText: "Propose a balance adjustment" })
    .getByRole("button", { name: "Submit for approval" });
  await expect(submit).toBeEnabled();

  // Not clicked. Everything above this line is what the operator sees before
  // deciding, which is what this spec is about; clicking would write a record
  // the schema will not let a test remove.
});
