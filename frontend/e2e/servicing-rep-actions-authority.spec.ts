import { test, expect } from "@playwright/test";
import { signInAsStaff, dbClient } from "./fixtures";
import type { Client } from "pg";

/**
 * Who is offered the proposal forms on a loan page, and on whose say-so.
 *
 * `specs/0002` grants "Raise a proposal" to csr, underwriter AND admin -- "any
 * staff member may ask" -- and both server layers agree. The browser decided it
 * from `localStorage`, and got the set wrong as well.
 *
 * The borrower case is the one that matters. This page admits borrowers by
 * design (they arrive from /my-loan), so a cached role is an identity claim
 * made by the person it is about.
 */

test.describe.configure({ timeout: 120_000 });

async function aLoanWithBalances(client: Client): Promise<number> {
  const res = await client.query(
    `SELECT l.id FROM loans l JOIN balances b ON b.loan_id = l.id
      WHERE l.status = 'current' ORDER BY l.id LIMIT 1`);
  expect(res.rows.length, "a serviced 'current' loan must exist").toBe(1);
  return res.rows[0].id;
}

/** The serviced loan belonging to a given applicant -- see the borrower test. */
async function aLoanOwnedBy(client: Client, applicantId: number): Promise<number> {
  const res = await client.query(
    `SELECT l.id FROM loans l
       JOIN applications a ON a.id = l.app_id
       JOIN balances b ON b.loan_id = l.id
      WHERE a.applicant_id = $1 AND l.status = 'current'
      ORDER BY l.id LIMIT 1`, [applicantId]);
  expect(res.rows.length,
    `applicant ${applicantId} must own a serviced 'current' loan for the borrower case`).toBe(1);
  return res.rows[0].id;
}

const ADJUST = "Propose a balance adjustment";
const WAIVE = "Propose a fee waiver";

test("an underwriter is offered the proposal forms, as the role matrix says", async ({ page }) => {
  // specs/0002: "Raise a proposal | csr | underwriter | admin -- any staff
  // member may ask". The UI used to admit csr/admin only, so an underwriter
  // was denied something the spec grants and the server permits.
  const client = dbClient();
  await client.connect();
  try {
    const loanId = await aLoanWithBalances(client);
    await signInAsStaff(page, "underwriter");
    await page.goto(`/servicing/${loanId}`);
    await expect(page.getByText(ADJUST)).toBeVisible({ timeout: 15_000 });
    await expect(page.getByText(WAIVE)).toBeVisible();
  } finally {
    await client.end();
  }
});

test("a CSR is still offered them", async ({ page }) => {
  // Guard the guard: if the forms were hidden from everyone the borrower test
  // below would pass for the wrong reason.
  const client = dbClient();
  await client.connect();
  try {
    const loanId = await aLoanWithBalances(client);
    await signInAsStaff(page, "csr");
    await page.goto(`/servicing/${loanId}`);
    await expect(page.getByText(ADJUST)).toBeVisible({ timeout: 15_000 });
  } finally {
    await client.end();
  }
});

test("a borrower who rewrites their cached role is offered no money controls", async ({ page }) => {
  // The finding. A borrower reaches this page legitimately, so tampering with
  // `meridian.user.role` is a claim about themselves made by themselves. The
  // gateway refuses a non-staff caller on these routes regardless -- nothing
  // could have moved -- but a rendered control promises an authority the
  // caller does not have, which is the defect the approvals queue was
  // corrected for (APQ-003) and is worse on a page borrowers are meant to use.
  // `maria` is the seeded borrower login (db/init/002_seed.sql, applicant_id 1)
  // -- a real borrower session, which is what makes the tamper meaningful: the
  // bearer token genuinely says "borrower" while the cache is made to say csr.
  // `signInAsStaff` is a sign-in helper, not a staff-only one.
  await signInAsStaff(page, "maria");

  const client = dbClient();
  await client.connect();
  try {
    // The loan MARIA OWNS, not merely the first serviced loan. The gateway
    // enforces owner-or-staff on `GET /lss/loans/{id}`, so a borrower sent to
    // someone else's loan is refused and the page never renders -- the test
    // would then pass because nothing loaded, proving nothing about who is
    // offered the controls. That holds locally by luck (maria owns the
    // lowest-numbered serviced loan here) and would not survive a reseed.
    const loanId = await aLoanOwnedBy(client, 1);

    await page.evaluate(() => {
      const raw = window.localStorage.getItem("meridian.user");
      if (!raw) throw new Error("no cached user to tamper with");
      const user = JSON.parse(raw);
      user.role = "csr"; // claim staff; the bearer token still says otherwise
      window.localStorage.setItem("meridian.user", JSON.stringify(user));
    });

    await page.goto(`/servicing/${loanId}`);

    // Anchor FIRST, and this is not decoration. `toHaveCount(0)` succeeds the
    // instant it is evaluated on an empty page, so asserting absence straight
    // after a navigation passes before anything has rendered -- which is
    // exactly how this test first passed against the un-fixed code. Waiting for
    // a section every borrower sees proves the page is loaded and that the
    // borrower can read this loan at all, so the absence below means the
    // controls were withheld rather than that nothing arrived.
    await expect(page.getByRole("heading", { name: "Make a payment" }))
      .toBeVisible({ timeout: 15_000 });

    await expect(page.getByText(ADJUST)).toHaveCount(0);
    await expect(page.getByText(WAIVE)).toHaveCount(0);
    await expect(page.getByRole("button", { name: "Submit for approval" })).toHaveCount(0);
  } finally {
    await client.end();
  }
});
