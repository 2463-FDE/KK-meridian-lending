import { test, expect } from "@playwright/test";
import { signInAsStaff, dbClient } from "./fixtures";
import type { Client } from "pg";

/**
 * Review finding APQ-002: the approvals queue's own tests seeded
 * `pending_movements` with SQL, so they could not see that no proposal could be
 * raised from a browser at all.
 *
 * This is that missing test. It drives the real servicing form and then asserts
 * the row the server actually wrote, so the assertion covers the whole contract
 * the UI has to satisfy -- the component vocabulary, the signed delta, and the
 * reason -- rather than only that a request was sent.
 *
 * Why it would have caught APQ-001. The form used to POST `{new_balance}`,
 * which `AdjustIn` rejects with 422 (`extra="forbid"`, and three required
 * fields absent). No row was written and the page reported success anyway, so
 * the only witness to the failure was the network tab.
 */

test.describe.configure({ timeout: 120_000 });

/**
 * A loan chosen by the STATE the proposal needs, not by "first row".
 *
 * The first draft took `ORDER BY id LIMIT 1` and got a loan whose fees are
 * negative, so servicing refused with "a movement of 25.0 would take fees below
 * zero" -- a correct refusal that made the test look like a UI bug. The
 * business rules are real and a fixture that ignores them tests nothing useful.
 */
async function aLoanWithBalances(client: Client): Promise<number> {
  const res = await client.query(
    `SELECT l.id FROM loans l JOIN balances b ON b.loan_id = l.id
      WHERE l.status = 'current' ORDER BY l.id LIMIT 1`);
  expect(res.rows.length, "a serviced 'current' loan must exist to propose against").toBe(1);
  return res.rows[0].id;
}

/** A loan whose fees can absorb `atLeast` -- a waiver cannot take fees below zero. */
async function aLoanWithFees(client: Client, atLeast: number): Promise<number> {
  const res = await client.query(
    `SELECT e.loan_id FROM ledger_entries e JOIN loans l ON l.id = e.loan_id
      WHERE e.component = 'fees' AND l.status = 'current'
      GROUP BY e.loan_id HAVING sum(e.amount) >= $1
      ORDER BY e.loan_id LIMIT 1`, [atLeast]);
  expect(res.rows.length,
    `no current loan carries at least ${atLeast} in fees, so a waiver cannot be tested`).toBe(1);
  return res.rows[0].loan_id;
}

/** Balances BEFORE and AFTER must be identical: a proposal moves no money. */
async function balanceOf(client: Client, loanId: number) {
  const res = await client.query(
    "SELECT balance, past_due FROM balances WHERE loan_id = $1", [loanId]);
  return res.rows[0] ?? null;
}

async function latestMovement(client: Client, loanId: number) {
  const res = await client.query(
    `SELECT id, component, amount, entry_type, reason, requested_role, resolution
       FROM pending_movements WHERE loan_id = $1 ORDER BY id DESC LIMIT 1`, [loanId]);
  return res.rows[0] ?? null;
}

test("a CSR can raise a balance-adjustment proposal from the servicing page", async ({ page }) => {
  const client = dbClient();
  await client.connect();
  let movementId: number | null = null;
  try {
    const loanId = await aLoanWithBalances(client);
    const before = await balanceOf(client, loanId);
    const reason = `e2e adjustment ${Date.now()}`;

    await signInAsStaff(page, "csr");
    await page.goto(`/servicing/${loanId}`);

    await expect(page.getByText("Propose a balance adjustment")).toBeVisible({ timeout: 15_000 });

    // The reason is required by the server and by this form: assert the button
    // stays disabled until it is given, so an operator cannot lose a typed
    // amount to a 422.
    await page.locator("#adjust-amount").fill("25.00");
    const submit = page.locator(".card", { hasText: "Propose a balance adjustment" })
      .getByRole("button", { name: "Submit for approval" });
    await expect(submit).toBeDisabled();

    await page.locator("#adjust-reason").fill(reason);
    await page.locator("#adjust-component").selectOption("principal");
    await expect(submit).toBeEnabled();
    await submit.click();

    // The copy must not claim the balance changed.
    await expect(page.getByText(/No money has moved/)).toBeVisible({ timeout: 15_000 });

    const row = await latestMovement(client, loanId);
    expect(row, "the form must have written a pending movement").not.toBeNull();
    movementId = row.id;
    expect(row.reason).toBe(reason);
    expect(row.component).toBe("principal");
    expect(Number(row.amount)).toBe(25);
    expect(row.entry_type).toBe("adjustment");
    expect(row.requested_role).toBe("csr");
    expect(row.resolution, "raising is not resolving").toBeNull();

    // The claim on screen, checked against the database rather than trusted.
    expect(await balanceOf(client, loanId)).toEqual(before);
  } finally {
    if (movementId !== null) {
      const admin = await client.query("SELECT id FROM users WHERE username = 'admin'");
      await client.query(
        "SELECT resolve_pending_movement($1, $2, 'admin', 'rejected', $3, $4)",
        [movementId, admin.rows[0].id, "500.00", ["current"]],
      );
    }
    await client.end();
  }
});

test("a fee waiver is sent as a negative amount, so the API accepts it", async ({ page }) => {
  // The operator types a positive figure -- "waive $15" -- and the client sends
  // -15, because `propose()` refuses a positive `fee_waived` outright ("a fee
  // waiver reduces what the borrower owes, so its amount is negative"). If the
  // page ever stops negating, this fails on the server's own refusal.
  const client = dbClient();
  await client.connect();
  let movementId: number | null = null;
  try {
    const loanId = await aLoanWithFees(client, 15);
    const before = await balanceOf(client, loanId);
    const reason = `e2e waiver ${Date.now()}`;

    await signInAsStaff(page, "csr");
    await page.goto(`/servicing/${loanId}`);

    await expect(page.getByText("Propose a fee waiver")).toBeVisible({ timeout: 15_000 });
    await page.locator("#waive-amount").fill("15.00");
    await page.locator("#waive-reason").fill(reason);
    await page.locator(".card", { hasText: "Propose a fee waiver" })
      .getByRole("button", { name: "Submit for approval" }).click();

    await expect(page.getByText(/No money has moved/)).toBeVisible({ timeout: 15_000 });

    const row = await latestMovement(client, loanId);
    expect(row).not.toBeNull();
    movementId = row.id;
    expect(row.reason).toBe(reason);
    expect(row.entry_type).toBe("fee_waived");
    expect(row.component).toBe("fees");
    expect(Number(row.amount), "a waiver is negative -- it reduces what is owed").toBe(-15);
    expect(await balanceOf(client, loanId)).toEqual(before);
  } finally {
    if (movementId !== null) {
      const admin = await client.query("SELECT id FROM users WHERE username = 'admin'");
      await client.query(
        "SELECT resolve_pending_movement($1, $2, 'admin', 'rejected', $3, $4)",
        [movementId, admin.rows[0].id, "500.00", ["current"]],
      );
    }
    await client.end();
  }
});
