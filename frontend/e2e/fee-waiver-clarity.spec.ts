import { test, expect } from "@playwright/test";
import type { Page } from "@playwright/test";
import type { Client } from "pg";
import {
  createFixtureLoan,
  dbClient,
  retireFixtureLoans,
  signInAsStaff,
} from "./fixtures";

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
 * A loan created for one test, not borrowed from a finite supply.
 *
 * Every other spec in this suite selects `ORDER BY ... LIMIT 1`, so they all
 * share the lowest-id serviced loan and mutate it: `approval-queue-self-approval`
 * has an admin resolve a movement against it, and the borrower payment specs
 * post against it too. This file cannot share a loan at all -- each case needs
 * an EXACT fee balance, and it puts that balance on through a `fee_assessed`
 * ledger entry, which is permanent.
 *
 * So it used to take an untouched loan from a band past the ids the rest of the
 * suite reaches, skipping any loan already carrying a ledger entry or a pending
 * movement. That made the file re-runnable and it CONSUMED the band: five loans
 * per run, and after roughly fifteen local runs against the same persistent
 * database the band was empty and every case failed with `no untouched serviced
 * loan left in the reserved band -- reseed the database` (RF-27, observed twice
 * during the pre-freeze audit).
 *
 * A created loan has no supply to draw down, so the file is repeatable with no
 * reseed. What did NOT fix it, and is worth naming because each looks like a
 * fix: a wider OFFSET (a bigger band still empties), a retry (there is nothing
 * to retry -- the loans are gone), a sleep (nothing is racing), randomising the
 * pick (it collides sooner rather than later), and depending on a reseed (which
 * is the workaround, not the fix).
 *
 * `createFixtureLoan` seeds `past_due` at 0 deliberately -- see its own note --
 * so `withFees` below lands on exactly the figure it asserts.
 */
const FIXTURE_LABEL = "fee-waiver";

async function aDedicatedLoan(): Promise<number> {
  return withDb(async (c) => (await createFixtureLoan(c, FIXTURE_LABEL)).loanId);
}

test.afterAll(async () => {
  await withDb((c) => retireFixtureLoans(c, FIXTURE_LABEL));
});


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
  const loanId = await aDedicatedLoan();

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
  const loanId = await aDedicatedLoan();

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
  const loanId = await aDedicatedLoan();

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
  const loanId = await aDedicatedLoan();

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
  const loanId = await aDedicatedLoan();

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
