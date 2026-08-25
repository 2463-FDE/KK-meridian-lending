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
 * Set a loan's fee balance to an exact figure and give it back afterwards.
 *
 * Written directly rather than through an assessed fee, and the reason is the
 * schema: `assess_late_fee` prices a fee off arrears, so producing a chosen fee
 * balance through it means first producing arrears, and the seeded portfolio has
 * none. `balances.past_due` is the fees projection (db/migrations/0035), and
 * setting it is the smallest way to put the page in front of a known number.
 *
 * The `meridian.projecting` guard function exists but is not attached to a
 * trigger yet (ADR 0010 step 5), so a direct UPDATE is still possible; the 0035
 * capture trigger records a `legacy_direct_write` ledger entry for the delta,
 * which is exactly what it is for. Restored to the original value at the end, so
 * the net effect on the demo data is nil.
 */
async function withFeeBalance<T>(
  loanId: number,
  fees: string,
  fn: () => Promise<T>,
): Promise<T> {
  const before = await withDb(
    async (c) =>
      (
        await c.query(
          "SELECT COALESCE(past_due, 0)::text AS past_due FROM balances WHERE loan_id = $1",
          [loanId],
        )
      ).rows[0].past_due,
  );
  await withDb((c) =>
    c.query("UPDATE balances SET past_due = $2 WHERE loan_id = $1", [loanId, fees]),
  );
  try {
    return await fn();
  } finally {
    await withDb((c) =>
      c.query("UPDATE balances SET past_due = $2 WHERE loan_id = $1", [loanId, before]),
    );
  }
}

async function aServicedLoan(): Promise<number> {
  const row = await withDb(
    async (c) =>
      (
        await c.query(
          `SELECT b.loan_id FROM balances b JOIN loans l ON l.id = b.loan_id
            WHERE l.status = 'current' ORDER BY b.loan_id LIMIT 1`,
        )
      ).rows[0],
  );
  if (!row) throw new Error("no serviced loan to propose a waiver against");
  return row.loan_id;
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
  const loanId = await aServicedLoan();

  await withFeeBalance(loanId, "0.00", async () => {
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
  const loanId = await aServicedLoan();

  await withFeeBalance(loanId, "500.00", async () => {
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
  const loanId = await aServicedLoan();

  await withFeeBalance(loanId, "350.00", async () => {
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
  const loanId = await aServicedLoan();

  await withFeeBalance(loanId, "100.00", async () => {
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
  const loanId = await aServicedLoan();

  await withFeeBalance(loanId, "500.00", async () => {
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
