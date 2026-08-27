import { test, expect } from "@playwright/test";
import type { Page } from "@playwright/test";
import type { Client } from "pg";
import { dbClient, signInAsStaff } from "./fixtures";

/**
 * Proposing a fee waiver shows what is owed, what can be waived, and where the
 * balance lands.
 *
 * A waiver removes an EXISTING fee. It is not a credit, not a principal
 * reduction, not a refund and not a payment -- so what can be waived is bounded
 * by what is owed. The form did not show that number, so an operator could type
 * 350 against 0.00 of fees, the client would send -350, and the server would
 * refuse it with "the ledger cannot hold a negative fees". The refusal was
 * right; the form should not have offered the request.
 *
 * **The positive-in / negative-out boundary is unchanged.** The operator types a
 * positive figure and the client sends a negative delta, because a waiver
 * reduces what is owed and the API's signed-ledger contract is what it is.
 * Asking an operator to type "-350" invites the sign error the refusal exists to
 * catch.
 *
 * **This is a usability guard, not the control.** Three server-side layers
 * already refuse an impossible waiver and all three stay: `maker_checker.propose`
 * at proposal time, `resolve_pending_movement` re-reading under lock at approval
 * time, and the ledger's own constraints. The server-bypass case is asserted in
 * `services/servicing-service/tests/` rather than here, where a browser cannot
 * skip the browser.
 *
 * Grouped by page load rather than one test per assertion: the gateway
 * rate-limits 120 requests per 60 seconds and a sign-in plus page load per
 * assertion exceeded it, which surfaced as staff forms that never rendered.
 */

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

/**
 * A loan reserved for this spec alone.
 *
 * Every other spec in this suite selects `ORDER BY ... LIMIT 1`, so they all
 * share the lowest-id serviced loan and mutate it: `approval-queue-self-approval`
 * has an admin resolve a movement against it, and the borrower payment specs
 * post against it too. This file took that same loan, so its fee balance was
 * whatever the specs before it had left behind. A fixed offset past the ids
 * every other spec reaches gives each test here its own seeded loan instead.
 *
 * Ascending order is what makes the choice stable: specs that board new loans
 * append higher ids, which cannot shift a low offset.
 *
 * The band is a floor, not the whole answer -- the query also SKIPS any loan
 * that already carries a ledger entry or a pending movement. That is what makes
 * the file re-runnable against a database it has already run on, and it means a
 * loan another spec reaches is stepped over rather than fought over. If the band
 * is ever exhausted the error says to reseed, which is a statement about the
 * data rather than a failed assertion about the code.
 */
const RESERVED_OFFSET = 100;

async function aDedicatedLoan(index: number): Promise<number> {
  return withDb(async (c) => {
    // The floor of the reserved band, resolved once so the band is defined by a
    // position rather than by an id this file would have to hard-code.
    const floorRow = (
      await c.query(
        `SELECT b.loan_id FROM balances b JOIN loans l ON l.id = b.loan_id
          WHERE l.status = 'current' ORDER BY b.loan_id OFFSET $1 LIMIT 1`,
        [RESERVED_OFFSET],
      )
    ).rows[0];
    if (!floorRow) {
      throw new Error(
        `fewer than ${RESERVED_OFFSET + 1} serviced loans: the reserved band does not exist`,
      );
    }

    // Untouched loans only, so a second run on the same database takes the next
    // ones along instead of inheriting the fees and proposals the first run
    // left. The ledger is append-only and a fee assessment cannot be reversed
    // without a proposal and a second approver, so "give the loan back" is not
    // available -- taking a fresh one is.
    const row = (
      await c.query(
        `SELECT b.loan_id FROM balances b JOIN loans l ON l.id = b.loan_id
          WHERE l.status = 'current'
            AND b.loan_id >= $1
            AND NOT EXISTS (SELECT 1 FROM ledger_entries e
                             WHERE e.loan_id = b.loan_id
                               AND e.entry_type <> 'opening_balance')
            AND NOT EXISTS (SELECT 1 FROM pending_movements m
                             WHERE m.loan_id = b.loan_id)
          ORDER BY b.loan_id OFFSET $2 LIMIT 1`,
        [Number(floorRow.loan_id), index],
      )
    ).rows[0];
    if (!row) {
      throw new Error(
        "no untouched serviced loan left in the reserved band -- reseed the database",
      );
    }
    return Number(row.loan_id);
  });
}


/**
 * Put an exact fee balance on the loan, through the ledger that owns it.
 *
 * `fee_assessed` is how fees come into existence -- the same path
 * `servicing-raises-a-proposal.spec.ts` already uses. The projection trigger
 * then writes `balances.past_due`, so the page reads a figure the ledger can
 * account for and the parity trigger stays satisfied. This used to be a direct
 * `UPDATE balances SET past_due`, which wrote the projection column without
 * going through the entry that justifies it.
 *
 * There is no restore, and that is deliberate: the loan belongs to this test, so
 * there is nothing to give back. The ledger is append-only, and reversing an
 * assessment would need a proposal and a second approver -- which is a different
 * test, not a fixture.
 */
async function withFees<T>(
  loanId: number,
  fees: string,
  fn: () => Promise<T>,
): Promise<T> {
  const amount = Number(fees);
  await withDb(async (c) => {
    if (amount > 0) {
      await c.query(
        `INSERT INTO ledger_entries (loan_id, component, amount, entry_type, reason)
         VALUES ($1, 'fees', $2, 'fee_assessed', 'e2e fixture: fees to waive')`,
        [loanId, fees],
      );
    }
    const owed = (
      await c.query(
        "SELECT COALESCE(past_due, 0)::text AS past_due FROM balances WHERE loan_id = $1",
        [loanId],
      )
    ).rows[0].past_due;
    expect(
      Number(owed),
      `the fixture must leave exactly ${fees} of fees owed on loan ${loanId}`,
    ).toBe(amount);
  });
  return fn();
}

async function openStaffLoanPage(page: Page, loanId: number): Promise<void> {
  await page.goto(`/servicing/${loanId}`);
  await expect(
    page.getByRole("heading", { name: "Servicing rep actions" }),
  ).toBeVisible({ timeout: 60_000 });
  await expect(page.getByTestId("waive-context")).toBeVisible({ timeout: 60_000 });
}

const waiverCard = (page: Page) =>
  page.locator(".card", { hasText: "Propose a fee waiver" });

test("with no fees owed, the form says so and cannot be submitted", async ({
  page,
}) => {
  const loanId = await aDedicatedLoan(0);

  await withFees(loanId, "0.00", async () => {
    await signInAsStaff(page, "csr");

    // Nothing may reach the proposal endpoint. Recorded rather than assumed:
    // the point is that the browser does not ASK for something impossible.
    let proposalCalls = 0;
    await page.route(`**/lss/accounts/${loanId}/waive-fee`, (route) => {
      proposalCalls += 1;
      return route.abort();
    });

    await openStaffLoanPage(page, loanId);

    const context = page.getByTestId("waive-context").locator(".dl-row");
    await expect(context.nth(0).locator("dd")).toHaveText(usdText(0));
    await expect(context.nth(1).locator("dd")).toHaveText(usdText(0));
    await expect(page.getByTestId("waive-nothing-to-waive")).toBeVisible();

    // Even with a plausible amount and a reason typed, submit stays disabled.
    await page.locator("#waive-amount").fill("350");
    await page.locator("#waive-reason").fill("e2e zero-fees check");
    await expect(
      waiverCard(page).getByRole("button", { name: "Submit for approval" }),
    ).toBeDisabled();
    // And no preview, because there is no valid outcome to preview.
    await expect(page.getByTestId("waive-preview")).toHaveCount(0);

    expect(proposalCalls).toBe(0);
  });
});

test("a partial waiver previews against the fee balance and is proposable", async ({
  page,
}) => {
  const loanId = await aDedicatedLoan(1);

  await withFees(loanId, "500.00", async () => {
    await signInAsStaff(page, "csr");
    await openStaffLoanPage(page, loanId);

    const context = page.getByTestId("waive-context").locator(".dl-row");
    await expect(context.nth(0).locator("dd")).toHaveText(usdText(500));
    await expect(context.nth(1).locator("dd")).toHaveText(usdText(500));
    await expect(page.getByTestId("waive-nothing-to-waive")).toHaveCount(0);

    await page.locator("#waive-amount").fill("350");
    await page.locator("#waive-reason").fill("e2e partial waiver");

    const preview = page.getByTestId("waive-preview").locator(".dl-row");
    await expect(preview.nth(0).locator("dd")).toHaveText(usdText(500));
    await expect(preview.nth(1).locator("dd")).toHaveText(`−${usdText(350)}`);
    await expect(preview.nth(2).locator("dd")).toHaveText(usdText(150));

    await expect(
      waiverCard(page).getByRole("button", { name: "Submit for approval" }),
    ).toBeEnabled();

    // The wording that keeps the preview honest: a snapshot, revalidated later.
    await expect(page.getByText(/Maximum currently waivable/i)).toBeVisible();
    await expect(
      page.getByText(/revalidated again at approval time/i),
    ).toBeVisible();
  });
});

test("waiving exactly the fee balance is allowed and lands on zero", async ({
  page,
}) => {
  /** The boundary. `component_now + delta < 0` is the server's condition, so
   *  landing exactly on zero is legal -- waiving the last cent of a fee balance
   *  is not an error, and a guard that used `>=` would forbid the commonest
   *  waiver of all. */
  const loanId = await aDedicatedLoan(2);

  await withFees(loanId, "350.00", async () => {
    await signInAsStaff(page, "csr");
    await openStaffLoanPage(page, loanId);

    await page.locator("#waive-amount").fill("350");
    await page.locator("#waive-reason").fill("e2e full waiver");

    const preview = page.getByTestId("waive-preview").locator(".dl-row");
    await expect(preview.nth(2).locator("dd")).toHaveText(usdText(0));
    await expect(page.getByTestId("waive-exceeds-fees")).toHaveCount(0);
    await expect(
      waiverCard(page).getByRole("button", { name: "Submit for approval" }),
    ).toBeEnabled();
  });
});

test("an over-waiver is refused before it is sent, and names the ceiling", async ({
  page,
}) => {
  const loanId = await aDedicatedLoan(3);

  await withFees(loanId, "100.00", async () => {
    await signInAsStaff(page, "csr");

    let proposalCalls = 0;
    await page.route(`**/lss/accounts/${loanId}/waive-fee`, (route) => {
      proposalCalls += 1;
      return route.abort();
    });

    await openStaffLoanPage(page, loanId);

    await page.locator("#waive-amount").fill("350");
    await page.locator("#waive-reason").fill("e2e over-waiver");

    const refusal = page.getByTestId("waive-exceeds-fees");
    await expect(refusal).toBeVisible();
    await expect(refusal).toContainText(usdText(100));
    await expect(refusal).toContainText(/negative fee balance/i);

    // No preview of an impossible outcome. "After approval -$250.00" would be a
    // state the server refuses, presented as a result.
    await expect(page.getByTestId("waive-preview")).toHaveCount(0);
    await expect(
      waiverCard(page).getByRole("button", { name: "Submit for approval" }),
    ).toBeDisabled();

    expect(proposalCalls).toBe(0);
  });
});

test("a partial or non-numeric amount reads as no proposal, not as NaN", async ({
  page,
}) => {
  const loanId = await aDedicatedLoan(4);

  await withFees(loanId, "500.00", async () => {
    await signInAsStaff(page, "csr");
    await openStaffLoanPage(page, loanId);

    const amount = page.locator("#waive-amount");
    await page.locator("#waive-reason").fill("e2e partial input");
    const submit = waiverCard(page).getByRole("button", {
      name: "Submit for approval",
    });

    // Nothing typed.
    await expect(submit).toBeDisabled();
    await expect(page.locator("body")).not.toContainText("NaN");

    // A decimal point mid-number. Typed rather than filled: the browser refuses
    // an incomplete value for `<input type="number">`, so `fill` would fail the
    // element's own validation instead of exercising the page.
    await amount.pressSequentially("0.");
    await expect(page.locator("body")).not.toContainText("NaN");
    await expect(submit).toBeDisabled();

    // Zero is not a waiver.
    await amount.fill("0");
    await expect(submit).toBeDisabled();
  });
});
