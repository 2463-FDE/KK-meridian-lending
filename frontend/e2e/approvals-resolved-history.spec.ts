import { test, expect } from "@playwright/test";
import { signInAsStaff, dbClient } from "./fixtures";
import type { Client } from "pg";

/**
 * A proposal that has been decided still has somewhere to be.
 *
 * Before this, `/approvals` rendered one list: unresolved proposals. The moment
 * a second person approved or rejected one it left that list and no screen
 * anywhere showed it again -- so an operator who raised a movement and came
 * back could not tell a rejection from a proposal that had never saved. The
 * control was working and its outcome was invisible.
 *
 * The page now has two named parts, and this spec pins what each one means:
 *
 *   [ Pending ]           work outstanding -- what is waiting on somebody
 *   [ Recently resolved ] what happened, and the evidence it produced
 *
 * The evidence is the reason the second list is worth having. `ledger_entry_id`
 * is written by the same transaction that writes the ledger entry, so its
 * presence or absence IS the account of whether money moved -- an approval
 * links to the entry, a rejection says plainly that none exists. A status word
 * alone could drift from the ledger; the id cannot.
 *
 * How each case is set up, and why they differ:
 *
 *   - The REJECTION runs against the loan every approvals spec shares. It moves
 *     no money and leaves nothing permanent behind, so it is safe there.
 *   - The APPROVAL writes a real, permanent ledger entry, so it runs against a
 *     reserved untouched loan and knowingly consumes one (RF-27).
 *
 * The approval could not be faked, and that is worth stating. The first attempt
 * seeded an already-resolved row pointing at an existing entry; the database
 * refused it -- "an approval may only link the entry it authorised". The link
 * this spec renders therefore cannot exist unless a second person really did
 * authorise the movement that produced it, which is the property that makes the
 * panel evidence rather than decoration.
 */

test.describe.configure({ timeout: 120_000 });

const SMALL_AMOUNT = "25.00"; // below the admin threshold, so authority is
                              // never what refuses -- this spec is about what
                              // the page SHOWS, not about who may resolve.

const PENDING = '[data-testid="approvals-pending"]';
const RESOLVED = '[data-testid="approvals-resolved"]';

async function userId(client: Client, username: string): Promise<number> {
  const res = await client.query("SELECT id FROM users WHERE username = $1", [username]);
  expect(res.rows.length, `seeded user ${username} must exist`).toBe(1);
  return res.rows[0].id;
}

async function aCurrentLoan(client: Client): Promise<number> {
  const res = await client.query(
    "SELECT id FROM loans WHERE status = 'current' ORDER BY id LIMIT 1",
  );
  expect(res.rows.length, "a loan with status 'current' must exist").toBe(1);
  return res.rows[0].id;
}

async function raiseProposal(
  client: Client,
  loanId: number,
  by: number,
  role: string,
): Promise<number> {
  const res = await client.query(
    `INSERT INTO pending_movements
       (loan_id, component, amount, entry_type, reason, requested_by, requested_role)
     VALUES ($1, 'fees', $2, 'adjustment', $3, $4, $5)
     RETURNING id`,
    [loanId, SMALL_AMOUNT, `e2e resolved-history check ${Date.now()}`, by, role],
  );
  return res.rows[0].id;
}

/**
 * A loan in the reserved band that nothing has touched yet.
 *
 * The approval below writes a real, permanent ledger entry, so it must not land
 * on the low-id loan every other spec shares. Same reserved-band mechanism as
 * `fee-waiver-clarity.spec.ts`, and the same accepted cost (RF-27): a run
 * consumes one untouched loan, and an exhausted band says to reseed rather than
 * failing as though the code were wrong.
 */
const RESERVED_OFFSET = 100;

async function aDedicatedLoan(client: Client): Promise<number> {
  const floorRow = (
    await client.query(
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
  const row = (
    await client.query(
      `SELECT b.loan_id FROM balances b JOIN loans l ON l.id = b.loan_id
        WHERE l.status = 'current'
          AND b.loan_id >= $1
          AND NOT EXISTS (SELECT 1 FROM ledger_entries e
                           WHERE e.loan_id = b.loan_id
                             AND e.entry_type <> 'opening_balance')
          AND NOT EXISTS (SELECT 1 FROM pending_movements m
                           WHERE m.loan_id = b.loan_id)
        ORDER BY b.loan_id LIMIT 1`,
      [Number(floorRow.loan_id)],
    )
  ).rows[0];
  if (!row) {
    throw new Error("no untouched serviced loan left in the reserved band -- reseed the database");
  }
  return Number(row.loan_id);
}

async function resolutionOf(client: Client, id: number) {
  const res = await client.query(
    "SELECT resolution, ledger_entry_id FROM pending_movements WHERE id = $1",
    [id],
  );
  return res.rows[0];
}

test("both sections are named, so a decided proposal has somewhere to be", async ({ page }) => {
  await signInAsStaff(page, "admin");
  await page.goto("/approvals");

  await expect(page.getByTestId("approvals-pending-heading")).toBeVisible({ timeout: 20_000 });
  await expect(page.getByTestId("approvals-resolved-heading")).toBeVisible();
  await expect(page.getByTestId("approvals-pending-heading")).toHaveText("Pending");
  await expect(page.getByTestId("approvals-resolved-heading")).toHaveText("Recently resolved");
});

test("a rejected proposal moves out of Pending and into Recently resolved", async ({ page }) => {
  const client = dbClient();
  await client.connect();
  try {
    const loanId = await aCurrentLoan(client);
    const underwriterId = await userId(client, "underwriter");
    const adminId = await userId(client, "admin");
    const movementId = await raiseProposal(client, loanId, underwriterId, "underwriter");

    await signInAsStaff(page, "admin");
    await page.goto("/approvals");

    const pendingCard = page.locator(`${PENDING} section.card`, {
      hasText: `Movement ${movementId}`,
    });
    await expect(pendingCard).toBeVisible({ timeout: 20_000 });
    await pendingCard.getByRole("button", { name: "Reject" }).click();

    // It left the queue...
    await expect(pendingCard).toHaveCount(0, { timeout: 20_000 });

    // ...and it is accounted for rather than gone.
    const resolvedCard = page.getByTestId(`resolved-movement-${movementId}`);
    await expect(resolvedCard).toBeVisible({ timeout: 20_000 });
    await expect(page.getByTestId(`resolution-${movementId}`)).toHaveText("Rejected");
    // The second person, named. That a DIFFERENT person resolved it is the
    // whole control, so "resolved" on its own would not show it had held.
    await expect(resolvedCard).toContainText(`Rejected by user ${adminId} (admin)`);
    await expect(resolvedCard).toContainText(`Raised by user ${underwriterId} (underwriter)`);
    // The honest absence. A rejection produced no entry, and the page says so
    // rather than leaving a blank where evidence would go.
    await expect(resolvedCard).toContainText("No ledger entry");
  } finally {
    await client.end();
  }
});

test("an approved movement links to the ledger entry it produced", async ({ page }) => {
  // This one APPROVES, and so writes a real ledger entry. Every other approvals
  // spec rejects on purpose -- the ledger is append-only and an approval cannot
  // be given back -- so this runs against a reserved, untouched loan and pays
  // the RF-27 cost knowingly.
  //
  // It has to approve. The link under test is not decoration: a database
  // trigger refuses a `ledger_entry_id` on a movement the entry does not name
  // ("an approval may only link the entry it authorised"), so a seeded row
  // pointing at some existing entry is rejected outright. The only way to have
  // a genuine link to render is to have genuinely authorised one, which is
  // exactly the property worth pinning.
  const client = dbClient();
  await client.connect();
  try {
    const loanId = await aDedicatedLoan(client);
    const underwriterId = await userId(client, "underwriter");
    const movementId = await raiseProposal(client, loanId, underwriterId, "underwriter");

    await signInAsStaff(page, "admin");
    await page.goto("/approvals");

    const pendingCard = page.locator(`${PENDING} section.card`, {
      hasText: `Movement ${movementId}`,
    });
    await expect(pendingCard).toBeVisible({ timeout: 20_000 });
    await pendingCard.getByRole("button", { name: "Approve" }).click();

    // The notice is the first thing the approver reads, and it now offers the
    // evidence rather than only announcing that an entry exists somewhere.
    const notice = page.getByTestId("approvals-notice");
    await expect(notice).toContainText(`Movement ${movementId} approved`, { timeout: 20_000 });
    await expect(notice.getByRole("link", { name: "See it in Account activity" })).toHaveAttribute(
      "href",
      `/servicing/${loanId}#account-activity`,
    );

    // The database is the authority on what was written; the page is checked
    // against it rather than against itself.
    const row = await resolutionOf(client, movementId);
    expect(row.resolution).toBe("approved");
    expect(row.ledger_entry_id, "an approval writes a ledger entry").not.toBeNull();
    const entryId = Number(row.ledger_entry_id);

    const card = page.getByTestId(`resolved-movement-${movementId}`);
    await expect(card).toBeVisible({ timeout: 20_000 });
    await expect(page.getByTestId(`resolution-${movementId}`)).toHaveText("Approved");
    await expect(card).toContainText(`Ledger entry ${entryId}`);
    // Recorded at resolution time, not read from configuration now: a history
    // of approvals is unreadable if the bar moved and nothing says when.
    await expect(card).toContainText("Judged against");

    // The link is the point -- evidence you can open, not a number to retype.
    const link = card.getByRole("link", { name: "See it in Account activity" });
    await expect(link).toHaveAttribute("href", `/servicing/${loanId}#account-activity`);
    await link.click();

    await expect(page.getByTestId("account-activity")).toBeVisible({ timeout: 30_000 });
  } finally {
    await client.end();
  }
});

test("the pending queue is unchanged by the history beside it", async ({ page }) => {
  // The new section is a read. It must not take a proposal off the queue, and
  // it must not put a resolved one back on -- the two lists answer different
  // questions and a row belongs to exactly one of them.
  const client = dbClient();
  await client.connect();
  try {
    const loanId = await aCurrentLoan(client);
    const underwriterId = await userId(client, "underwriter");
    const movementId = await raiseProposal(client, loanId, underwriterId, "underwriter");

    await signInAsStaff(page, "admin");
    await page.goto("/approvals");

    await expect(
      page.locator(`${PENDING} section.card`, { hasText: `Movement ${movementId}` }),
    ).toBeVisible({ timeout: 20_000 });
    await expect(
      page.locator(`${RESOLVED} section.card`, { hasText: `Movement ${movementId}` }),
    ).toHaveCount(0);
  } finally {
    await client.end();
  }
});

test("a CSR can read the history it can see the queue for, and resolves neither", async ({
  page,
}) => {
  // Visibility is not authority. A CSR already reads the pending queue and gets
  // no buttons; hiding what happened to those same proposals from it would
  // protect nothing, so the history is readable on exactly the same terms.
  await signInAsStaff(page, "csr");
  await page.goto("/approvals");

  await expect(page.getByTestId("approvals-resolved-heading")).toBeVisible({ timeout: 20_000 });
  await expect(page.locator(`${PENDING}`).getByRole("button", { name: "Approve" })).toHaveCount(0);
  await expect(page.locator(`${RESOLVED}`).getByRole("button", { name: "Approve" })).toHaveCount(0);
  await expect(page.locator(`${RESOLVED}`).getByRole("button", { name: "Reject" })).toHaveCount(0);
});
