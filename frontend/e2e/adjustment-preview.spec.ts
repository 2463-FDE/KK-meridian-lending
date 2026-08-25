import { test, expect } from "@playwright/test";
import type { Page } from "@playwright/test";
import type { Client } from "pg";
import { dbClient, signInAsStaff } from "./fixtures";

/**
 * A staff member proposing a balance adjustment can see which direction they
 * have chosen, against which balance, and whether the server will accept it.
 *
 * The backend semantics are a SIGNED DELTA and stay that way: +450 means the
 * borrower owes $450 more. `new_balance` was removed deliberately in #35 (spec
 * 0002 REQ-VAL-1) -- "set the balance to X" invites a caller to overwrite a
 * figure it read some time ago. What was missing was not the semantics but any
 * way to read them off the screen: an operator typed into a field labelled
 * "Change (USD)" with no starting balance beside it and no statement of where
 * the number would land.
 *
 * **Four tests, not eight, and the reason is worth recording.** An earlier
 * version had one test per assertion, each signing in and loading the page. Run
 * repeatedly it tripped the gateway's rate limit -- `RATE_LIMIT_MAX_REQUESTS`
 * defaults to 120 per 60 seconds, and the gateway log showed 14 x HTTP 429 -- so
 * `/auth/me` failed, `canRepActions` stayed false, and the staff forms never
 * rendered. The symptom looked like flakiness and moved between tests, which is
 * exactly what RF-24's shared-database flakiness looks like; the gateway log is
 * what told them apart. Grouping assertions by page load halves the request
 * count and removes the cause rather than retrying past it.
 */

// Sign-in, navigation and the `/auth/me` round trip all precede the first
// assertion, and the first test in the file also pays the Next.js cold page
// compile. The default 30s budget is not enough for that; raised for the file so
// a new test here inherits it.
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

/** The page's own money formatting, so a positive assertion can be exact. */
function usdText(value: number): string {
  return value.toLocaleString("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: 2,
    maximumFractionDigits: 2,
  });
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
 * The difference is the whole requirement: it makes previewing against the wrong
 * component detectable instead of accidentally right. No `past_due > 0`
 * condition -- an earlier version had one and found nothing on a freshly seeded
 * portfolio, and a past-due of 0.00 beside a five-figure principal is the sharper
 * case anyway: a mis-paired preview shows thousands where it should show nothing.
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

/**
 * Open a loan's servicing page and wait until the STAFF section has rendered.
 *
 * The proposal forms are gated on the verified role from `/auth/me`, which
 * resolves after the first paint -- so `goto` then asserting on the preview
 * raced, and the preview came back "not found" on a page that had simply not
 * finished deciding what to show. 60s rather than 30s because the first load also
 * compiles the page, and a 30s inner wait inside a 90s budget gave up while the
 * budget was still running.
 */
async function openStaffLoanPage(page: Page, loanId: number): Promise<void> {
  await page.goto(`/servicing/${loanId}`);
  await expect(
    page.getByRole("heading", { name: "Servicing rep actions" }),
  ).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("adjust-preview")).toBeVisible({ timeout: 60_000 });
}

const adjustCard = (page: Page) =>
  page.locator(".card", { hasText: "Propose a balance adjustment" });

test("a principal change previews against principal, in both directions", async ({
  page,
}) => {
  const loan = await aLoanWithDistinctBalances();

  await signInAsStaff(page, "csr");
  await openStaffLoanPage(page, loan.loanId);

  const rows = page.getByTestId("adjust-preview").locator(".dl-row");
  const after = rows.nth(2).locator("dd");

  // The component's own balance, named for what it is.
  await expect(rows.nth(0).locator("dt")).toHaveText(/Current principal balance/i);
  expect(money(await rows.nth(0).locator("dd").textContent())).toBeCloseTo(
    loan.balance,
    2,
  );
  // Nothing typed reads as no change -- not as zero, and not as NaN.
  await expect(after).toHaveText("—");

  // Up.
  await page.locator("#adjust-amount").fill("450");
  await expect(after).toHaveText(usdText(loan.balance + 450));
  await expect(rows.nth(1).locator("dd")).toHaveText(/\+/);

  // Down. A plus and a minus are one character apart on screen and hundreds of
  // dollars apart in effect.
  await page.locator("#adjust-amount").fill("-450");
  await expect(after).toHaveText(usdText(loan.balance - 450));
  await expect(rows.nth(1).locator("dd")).not.toHaveText(/\+/);
});

test("a fees change previews against past-due fees, not principal", async ({
  page,
}) => {
  /** The mis-pairing this spec exists for. `balances.balance` is projected from
   *  `component = 'principal'` entries and `past_due` from `'fees'`
   *  (db/migrations/0035), so reaching for the wrong one is a real possibility,
   *  and the chosen loan makes it visible. */
  const loan = await aLoanWithDistinctBalances();
  expect(loan.pastDue).not.toBeCloseTo(loan.balance, 2);

  await signInAsStaff(page, "csr");
  await openStaffLoanPage(page, loan.loanId);

  await page.locator("#adjust-component").selectOption("fees");
  await page.locator("#adjust-amount").fill("10");

  const rows = page.getByTestId("adjust-preview").locator(".dl-row");
  await expect(rows.nth(0).locator("dt")).toHaveText(/Past-due fees now/i);
  expect(money(await rows.nth(0).locator("dd").textContent())).toBeCloseTo(
    loan.pastDue,
    2,
  );
  await expect(rows.nth(2).locator("dd")).toHaveText(usdText(loan.pastDue + 10));
});

test("a partial amount reads as no change rather than NaN", async ({ page }) => {
  const loan = await aLoanWithDistinctBalances();

  await signInAsStaff(page, "csr");
  await openStaffLoanPage(page, loan.loanId);

  const amount = page.locator("#adjust-amount");

  // Typed rather than filled. This is an `<input type="number">`: the browser
  // refuses "-" as a value, so `fill("-")` fails the element's own validation and
  // an earlier version of this test failed on the harness rather than on the
  // page. A person types the minus first all the same, and the control reports an
  // empty value while the number is incomplete -- which is the
  // `parseFloat("") -> NaN` the guard is for.
  await amount.pressSequentially("-");
  await expect(page.locator("body")).not.toContainText("NaN");

  // And a decimal point mid-number, the other partial state.
  await amount.fill("");
  await amount.pressSequentially("0.");
  await expect(page.locator("body")).not.toContainText("NaN");
});

test("a change below zero is refused, and the boundary at zero is not", async ({
  page,
}) => {
  /**
   * Review of PR #86, AP-001. On a portfolio where `past_due` is 0.00, Fees and
   * -10 rendered "After approval -$10.00" with submit still enabled -- the screen
   * telling an operator a request will work when `maker_checker.propose` refuses
   * it at proposal time (REQ-VAL-8/AC-20, `component_now + delta < 0`).
   *
   * A preview that shows a refused state as an approved outcome is worse than no
   * preview.
   */
  const loan = await aLoanWithDistinctBalances();

  await signInAsStaff(page, "csr");
  await openStaffLoanPage(page, loan.loanId);

  const rows = page.getByTestId("adjust-preview").locator(".dl-row");
  const after = rows.nth(2).locator("dd");
  const refusal = page.getByTestId("adjust-below-zero");
  const submit = adjustCard(page).getByRole("button", {
    name: "Submit for approval",
  });

  // The preview says what it is, before any amount is typed.
  await expect(
    page.getByText(/No money moves when this proposal is submitted/i),
  ).toBeVisible();
  await expect(
    page.getByText(/revalidated when a different authorized staff member approves/i),
  ).toBeVisible();

  await page.locator("#adjust-reason").fill("e2e preview check");

  // Below zero, derived from the loan's OWN past-due balance rather than
  // hardcoded. A fixed "-10" was permitted on the seeded data -- one loan carries
  // $25.00 of arrears -- and ten dollars past the balance is below zero whatever
  // the balance is.
  await page.locator("#adjust-component").selectOption("fees");
  await page.locator("#adjust-amount").fill(`-${loan.pastDue + 10}`);
  await expect(after).toHaveText(/Not permitted/i);
  await expect(refusal).toBeVisible();
  await expect(refusal).toContainText(/past-due fees/i);
  await expect(refusal).toContainText(/below zero/i);
  // And it cannot be submitted, so nobody queues a proposal that cannot be
  // approved.
  await expect(submit).toBeDisabled();

  // Above zero: permitted, and the refusal is gone. The POSITIVE assertion goes
  // first -- `toHaveCount(0)` on the refusal is satisfied before React has
  // re-rendered at all, so on its own it can pass for the wrong reason.
  await page.locator("#adjust-component").selectOption("principal");
  const half = Math.floor(loan.balance / 2);
  await page.locator("#adjust-amount").fill(`-${half}`);
  await expect(after).toHaveText(usdText(loan.balance - half));
  await expect(refusal).toHaveCount(0);
  await expect(submit).toBeEnabled();

  // Exactly zero is legal: `component_now + delta < 0` is the server's
  // condition, and paying the last cent of a balance is not an error.
  await page.locator("#adjust-amount").fill(`-${loan.balance}`);
  await expect(after).toHaveText(usdText(0));
  await expect(refusal).toHaveCount(0);
  await expect(submit).toBeEnabled();

  // Not clicked, deliberately, and the test is named for what it covers. An
  // earlier version was called "submitting the proposal reports that no money
  // moved" while stopping here, which promised a submission it never made.
  // Clicking would write a record `pending_movements_are_retained()` will not let
  // a test remove; the post-submit path is covered by
  // `servicing-raises-a-proposal.spec.ts` and by
  // `test_maker_checker_api.py::test_adjust_balance_raises_a_proposal_and_moves_nothing`.
});
