import { test, expect } from "@playwright/test";
import { signInAsStaff, dbClient } from "./fixtures";
import type { Client } from "pg";

/**
 * The approvals queue publishes three rules to staff. This spec exists because
 * each of them is a claim the browser makes on the server's behalf, and a UI
 * claim that the server does not actually enforce is the defect this repository
 * keeps producing in different costumes:
 *
 *   1. "You raised this -- a different approver is required."
 *      Enforced by `no_self_approval` (db/migrations/0036_pending_movements.sql)
 *      and by `resolve_pending_movement`. Asserted below both ways: the control
 *      is disabled AND the row is still unresolved afterwards.
 *
 *   2. "Your role can see this queue but not resolve it." (CSR)
 *      Enforced by maker_checker.APPROVER_ROLES_AT_OR_BELOW_THRESHOLD, which
 *      admits underwriter and admin only. specs/0002 role matrix: a CSR may not
 *      dispose of a proposal by rejecting it either.
 *
 *   3. "Nothing listed here has moved any money yet."
 *      Enforced by maker_checker.propose(), which writes one pending_movements
 *      row and touches neither balances nor ledger_entries.
 *
 * The admin path REJECTS rather than approves. Rejection exercises the same
 * authority decision and the same queue refresh, and it deliberately moves no
 * money: approving would write a real ledger entry against a seeded loan, and
 * the ledger is append-only, so this suite would leave a permanent adjustment
 * behind on every run. The approve path is covered against the database in
 * servicing-service's own tests.
 */

// The default 30s budget covers a whole test, and the first spec below signs in
// twice (raiser, then a different approver) with several database round-trips
// between. It was running out of budget mid-navigation, which reports as a
// `page.goto` timeout and looks like a hung page. No assertion or wait is
// relaxed by this -- only the envelope around them.
test.describe.configure({ timeout: 120_000 });

const SMALL_AMOUNT = "25.00"; // below MAKER_CHECKER_ADMIN_THRESHOLD, so the
                              // threshold rule is never what refuses -- this
                              // spec is about WHO is asking, not how much.

async function userId(client: Client, username: string): Promise<number> {
  const res = await client.query("SELECT id FROM users WHERE username = $1", [username]);
  expect(res.rows.length, `seeded user ${username} must exist`).toBe(1);
  return res.rows[0].id;
}

async function aCurrentLoan(client: Client): Promise<number> {
  const res = await client.query("SELECT id FROM loans WHERE status = 'current' ORDER BY id LIMIT 1");
  expect(res.rows.length, "a loan with status 'current' must exist to propose against").toBe(1);
  return res.rows[0].id;
}

async function raiseProposal(client: Client, loanId: number, by: number, role: string): Promise<number> {
  const res = await client.query(
    `INSERT INTO pending_movements
       (loan_id, component, amount, entry_type, reason, requested_by, requested_role)
     VALUES ($1, 'fees', $2, 'adjustment', $3, $4, $5)
     RETURNING id`,
    [loanId, SMALL_AMOUNT, `e2e approvals-queue check ${Date.now()}`, by, role],
  );
  return res.rows[0].id;
}

async function resolutionOf(client: Client, id: number) {
  const res = await client.query(
    "SELECT resolution, resolved_by, ledger_entry_id FROM pending_movements WHERE id = $1",
    [id],
  );
  return res.rows[0];
}

test("the approvals queue refuses self-approval in the browser, and the row stays unresolved", async ({ page }) => {
  const client = dbClient();
  await client.connect();
  try {
    const loanId = await aCurrentLoan(client);
    const underwriterId = await userId(client, "underwriter");
    const adminId = await userId(client, "admin");
    const movementId = await raiseProposal(client, loanId, underwriterId, "underwriter");

    // --- the raiser sees their own proposal and cannot resolve it -----------
    await signInAsStaff(page, "underwriter");
    await page.goto("/approvals");

    const card = page.locator("section.card", { hasText: `Movement ${movementId}` });
    await expect(card).toBeVisible({ timeout: 15_000 });
    await expect(card.getByText("Raised by you (underwriter)")).toBeVisible();
    await expect(card.getByText("You raised this")).toBeVisible();
    await expect(card.getByRole("button", { name: "Approve" })).toBeDisabled();
    await expect(card.getByRole("button", { name: "Reject" })).toBeDisabled();

    // The claim is about the database, not about a disabled attribute: a UI
    // that greys the button while the row quietly resolves would pass every
    // assertion above.
    let row = await resolutionOf(client, movementId);
    expect(row.resolution, "the raiser's own visit must not resolve anything").toBeNull();

    // --- a different approver CAN resolve it -------------------------------
    await signInAsStaff(page, "admin");
    await page.goto("/approvals");

    const adminCard = page.locator("section.card", { hasText: `Movement ${movementId}` });
    await expect(adminCard).toBeVisible({ timeout: 15_000 });
    await expect(adminCard.getByText(`Raised by user ${underwriterId} (underwriter)`)).toBeVisible();
    await expect(adminCard.getByRole("button", { name: "Approve" })).toBeEnabled();
    await adminCard.getByRole("button", { name: "Reject" }).click();

    await expect(page.getByText(`Movement ${movementId} rejected`)).toBeVisible({ timeout: 15_000 });

    row = await resolutionOf(client, movementId);
    expect(row.resolution).toBe("rejected");
    expect(Number(row.resolved_by)).toBe(adminId);
    expect(row.ledger_entry_id, "a rejection moves no money").toBeNull();

    // Resolved proposals leave the queue -- the queue is work outstanding.
    await expect(page.locator("section.card", { hasText: `Movement ${movementId}` })).toHaveCount(0);
  } finally {
    await client.end();
  }
});

test("a tampered cached role does not hand a CSR the resolve controls", async ({ page }) => {
  // Review finding APQ-003. The page used to read its own identity from
  // `getUser()` -- i.e. from localStorage, which anyone at the keyboard can
  // edit and which can outlive the session it describes. `RequireRole` does
  // verify the session against `GET /auth/me`, but it keeps only a yes/no, so
  // the role and id driving the buttons came from the cache regardless.
  //
  // Nothing here could actually move money: servicing decides authority from a
  // signed principal the browser cannot forge, and would refuse. But a screen
  // that OFFERS an authority the caller does not have is the precise failure
  // this page was built to remove, so the controls must follow the verified
  // answer, not the cached one.
  const client = dbClient();
  await client.connect();
  let movementId: number | null = null;
  try {
    const loanId = await aCurrentLoan(client);
    const underwriterId = await userId(client, "underwriter");
    movementId = await raiseProposal(client, loanId, underwriterId, "underwriter");

    await signInAsStaff(page, "csr");

    // Tamper with the cache the page used to trust: claim to be an admin.
    // The bearer token is left untouched, so the SERVER still sees a CSR --
    // which is exactly the disagreement the page has to resolve in the
    // server's favour.
    await page.evaluate(() => {
      const raw = window.localStorage.getItem("meridian.user");
      if (!raw) throw new Error("no cached user to tamper with");
      const user = JSON.parse(raw);
      user.role = "admin";
      user.id = 999999;
      window.localStorage.setItem("meridian.user", JSON.stringify(user));
    });

    await page.goto("/approvals");

    const card = page.locator("section.card", { hasText: `Movement ${movementId}` });
    await expect(card).toBeVisible({ timeout: 15_000 });
    // Verified role is still csr, so still no controls -- the tampered cache
    // changed nothing.
    await expect(card.getByRole("button", { name: "Approve" })).toHaveCount(0);
    await expect(card.getByRole("button", { name: "Reject" })).toHaveCount(0);
    await expect(card.getByText("Your role can see this queue but not resolve it")).toBeVisible();

    const row = await resolutionOf(client, movementId);
    expect(row.resolution, "a tampered cache must not resolve anything").toBeNull();
  } finally {
    if (movementId !== null) {
      const adminId = await userId(client, "admin");
      await client.query(
        "SELECT resolve_pending_movement($1, $2, 'admin', 'rejected', $3, $4)",
        [movementId, adminId, "500.00", ["current"]],
      );
    }
    await client.end();
  }
});

test("a CSR can read the approvals queue and is offered no way to resolve it", async ({ page }) => {
  const client = dbClient();
  await client.connect();
  let movementId: number | null = null;
  try {
    const loanId = await aCurrentLoan(client);
    const underwriterId = await userId(client, "underwriter");
    movementId = await raiseProposal(client, loanId, underwriterId, "underwriter");

    await signInAsStaff(page, "csr");
    await page.goto("/approvals");

    const card = page.locator("section.card", { hasText: `Movement ${movementId}` });
    await expect(card).toBeVisible({ timeout: 15_000 });
    // Visibility is not authority: the CSR sees the queue, and neither control
    // is rendered at all -- an enabled button here would promise an authority
    // the server refuses with a 403 only after the click.
    await expect(card.getByRole("button", { name: "Approve" })).toHaveCount(0);
    await expect(card.getByRole("button", { name: "Reject" })).toHaveCount(0);
    await expect(card.getByText("Your role can see this queue but not resolve it")).toBeVisible();

    const row = await resolutionOf(client, movementId);
    expect(row.resolution, "nothing a CSR can do on this page resolves a movement").toBeNull();
  } finally {
    // Left alone, this proposal would sit in the queue for ever and every run
    // would add another. It is NOT deleted: pending_movements refuses deletes
    // outright ("proposals are retained as the evidence of what staff asked
    // for"), and that refusal is correct -- a record of what someone asked for
    // is not disposable because a test made it. So it is closed the way a real
    // one would be: rejected, by a different person, through the sanctioned
    // function. That moves no money and keeps the evidence.
    if (movementId !== null) {
      const adminId = await userId(client, "admin");
      await client.query(
        "SELECT resolve_pending_movement($1, $2, 'admin', 'rejected', $3, $4)",
        [movementId, adminId, "500.00", ["current"]],
      );
    }
    await client.end();
  }
});
