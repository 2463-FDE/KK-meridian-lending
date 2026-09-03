import { test, expect } from "@playwright/test";
import {
  createFixtureLoan,
  dbClient,
  retireFixtureLoans,
  signInAsStaff,
} from "./fixtures";
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
 *     loan created for it. It used to draw one from a finite reserved band and
 *     consume it, which is the fixture defect RF-27 recorded.
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
 * A loan created for the approval case, not taken from a finite supply.
 *
 * The approval below writes a real, permanent ledger entry, so it must not land
 * on the low-id loan every other approvals spec shares. It used to take an
 * untouched loan from a band past the ids the rest of the suite reaches, and
 * consumed one per run (RF-27) -- the band emptied after roughly fifteen local
 * runs against the same persistent database and the case then failed with
 * `no untouched serviced loan left in the reserved band -- reseed the database`,
 * which is a statement about the fixture's supply rather than about the code.
 *
 * Creating the loan removes the supply, so there is nothing to exhaust. The
 * REJECTION case above is unchanged: it moves no money and leaves nothing
 * permanent behind, so the shared loan is still safe for it, and keeping it
 * there is what shows the two cases differ for a reason.
 */
const FIXTURE_LABEL = "approvals-resolved";

async function aDedicatedLoan(client: Client): Promise<number> {
  return (await createFixtureLoan(client, FIXTURE_LABEL)).loanId;
}

test.afterAll(async () => {
  const client = dbClient();
  await client.connect();
  try {
    await retireFixtureLoans(client, FIXTURE_LABEL);
  } finally {
    await client.end();
  }
});


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
  // `toContainText`, not `toHaveText`: each heading now carries its half's
  // count beside the name. The name is what this test is about; the count has
  // its own test below.
  await expect(page.getByTestId("approvals-pending-heading")).toContainText("Pending");
  await expect(page.getByTestId("approvals-resolved-heading")).toContainText(
    "Recently resolved",
  );
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
  // be given back -- so this runs against a loan created for it.
  //
  // It used to draw that loan from a finite reserved band and pay the RF-27
  // cost knowingly. It does not any more: `createFixtureLoan` makes one, so
  // there is no supply to exhaust and no cost to pay.
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

test("each half says how much of it is on the screen", async ({ page }) => {
  // The truncation defect, at the level a reader meets it. Both panels are
  // capped server-side (50 pending, 25 resolved), and until this they were
  // rendered under headings that read as the whole queue -- so past the cap a
  // real proposal waiting on a real approver was off the screen with nothing
  // saying more existed.
  //
  // This asserts the honest form is present, not a particular number: the
  // seeded stack holds far fewer proposals than either cap, so the count reads
  // "(N of N)". That is the point. "(7 of 7)" is a positive statement that
  // nothing is hidden; a bare "7" leaves the reader to assume it, and the
  // assumption is what was wrong.
  await signInAsStaff(page, "admin");
  await page.goto("/approvals");

  const pendingCount = page.getByTestId("approvals-pending-count");
  const resolvedCount = page.getByTestId("approvals-resolved-count");

  await expect(page.getByTestId("approvals-resolved-heading")).toBeVisible({
    timeout: 20_000,
  });

  // A half with nothing in it renders no count -- there is no queue to be
  // honest about. Whichever halves DO have rows must carry one.
  for (const [listId, countId] of [
    ["approvals-pending", pendingCount],
    ["approvals-resolved", resolvedCount],
  ] as const) {
    const rows = page.getByTestId(listId).locator("section");
    const shown = await rows.count();
    if (shown === 0) continue;
    await expect(countId).toBeVisible();
    // "(shown of total)", with total never smaller than what is displayed.
    const text = ((await countId.textContent()) ?? "").trim();
    const match = text.match(/^\((\d+) of (\d+)\)$/);
    expect(match, `count read "${text}", which is not "(shown of total)"`).toBeTruthy();
    const [, displayed, total] = match!;
    expect(Number(displayed)).toBe(shown);
    expect(Number(total)).toBeGreaterThanOrEqual(Number(displayed));
  }
});
