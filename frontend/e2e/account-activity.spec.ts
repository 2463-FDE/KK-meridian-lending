import { test, expect } from "@playwright/test";
import type { Client } from "pg";
import { dbClient, signInAsStaff, signInAsBorrower, SEEDED_BORROWER } from "./fixtures";

/**
 * Account activity, in the browser: one payment reads as one movement, the
 * figures are the server's, and a borrower sees only their own.
 *
 * The page has promised "account activity" in its subtitle since it was written
 * and had none. What makes this worth a browser test rather than only a service
 * test is the thing a borrower would misread: a $500 card payment writes up to
 * three ledger rows, and three rows on screen are three charges they never made.
 *
 * Everything asserted here is compared against the DATABASE or against the
 * endpoint, never against a figure this spec computed. The whole point of the
 * read model is that the browser does no accounting.
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

/** "−$250.00" -> 250 */
function money(text: string | null): number {
  if (!text) throw new Error("no text to read a money figure from");
  const cleaned = text.replace(/[^0-9.]/g, "");
  if (!cleaned) throw new Error(`no digits in ${JSON.stringify(text)}`);
  return Number(cleaned);
}

test("a staff member sees each ledger movement once", async ({ page }) => {
  const loanId = SEEDED_BORROWER.loanId;

  // How many movements the ledger actually holds, grouped the way the server
  // groups them: by payment id where there is one, by row otherwise.
  const expected = await withDb(
    async (c) =>
      Number(
        (
          await c.query(
            `SELECT count(*) AS n FROM (
               SELECT COALESCE('p' || payment_id, 'e' || id) AS k
                 FROM ledger_entries WHERE loan_id = $1
               GROUP BY 1
             ) grouped`,
            [loanId],
          )
        ).rows[0].n,
      ),
  );
  expect(expected).toBeGreaterThan(0);

  await signInAsStaff(page, "csr");
  await page.goto(`/servicing/${loanId}`);

  const activity = page.getByTestId("account-activity");
  await expect(activity).toBeVisible({ timeout: 20_000 });
  await expect(activity.locator("tbody tr")).toHaveCount(expected);
});

test("a three-component payment renders as one row, on a payload that has one", async ({
  page,
}) => {
  /**
   * The grouping defect, caught in the browser.
   *
   * **This replaced a test that read the split from the database, and CI is why.**
   * That version asked for a payment with ledger entries and asserted one came
   * back. Locally one always did -- earlier browser runs had created payments --
   * and on CI's freshly seeded database there are none at all, so it failed with
   * `Expected: 1, Received: 0`. A test whose subject is the RENDERING should not
   * depend on the seed having produced a particular payment; the row count
   * against whatever the ledger holds is asserted separately, above, and works
   * with zero.
   *
   * Rendering one row per COMPONENT instead of per movement passed every other
   * test in this file. On the seeded portfolio every payment goes wholly to
   * principal (no loan carries arrears), so per-component and per-movement
   * rendering produce identical output and the row counts and amounts agree
   * either way. The data cannot tell them apart.
   *
   * Producing a real split would mean assessing a fee first -- which needs a
   * non-zero past-due balance to assess against -- then two approvers, and it
   * would write records `pending_movements_are_retained()` will not let a test
   * remove. So the response is intercepted instead: a real three-component
   * payment, rendered by the real component. The rendering is what is under
   * test, and this is the shortest path to putting the actual shape in front of
   * it.
   */
  const payload = {
    loan_id: SEEDED_BORROWER.loanId,
    note: "Authoritative movements that changed this account, read from the immutable ledger.",
    items: [
      {
        id: "payment:9001",
        occurred_at: "2026-08-20T10:00:00+00:00",
        category: "payment",
        description: "Payment received",
        amount: -500,
        components: { fees: -25, interest: -75, principal: -400 },
        payment_id: 9001,
        provenance: "processor",
      },
    ],
  };

  await signInAsStaff(page, "csr");
  await page.route(`**/lss/loans/${SEEDED_BORROWER.loanId}/activity`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify(payload),
    }),
  );
  await page.goto(`/servicing/${SEEDED_BORROWER.loanId}`);

  const activity = page.getByTestId("account-activity");
  await expect(activity).toBeVisible({ timeout: 20_000 });

  // ONE row. Three would be three charges the borrower never made.
  await expect(activity.locator("tbody tr")).toHaveCount(1);

  const row = activity.locator("tbody tr").first();
  // The movement's own total -- the server's figure, not the sum of the parts
  // recomputed here.
  expect(money(await row.locator("td.num").textContent())).toBeCloseTo(500, 2);

  // The parts are named beneath it, each displayed rather than derived.
  const applied = row.getByText(/Applied to/i);
  await expect(applied).toBeVisible();
  const text = (await applied.textContent()) ?? "";
  for (const expected of ["Fees", "$25.00", "Interest", "$75.00", "Principal", "$400.00"]) {
    expect(text).toContain(expected);
  }
});

test("the borrower sees their own activity, and the principal label is precise", async ({
  page,
}) => {
  await signInAsBorrower(page);
  await page.goto("/my-loan");

  const activity = page.getByTestId("account-activity").first();
  await expect(activity).toBeVisible({ timeout: 20_000 });
  await expect(activity.locator("tbody tr").first()).toBeVisible();

  // `balances.balance` is projected only from `component = 'principal'` ledger
  // entries, so the summary card names it for what it holds. "Current balance"
  // read as everything owed, which it is not -- the fees are the row below.
  await expect(page.getByText("Current principal balance")).toBeVisible();
});

test("no staff reason, actor or correlation id reaches the borrower's screen", async ({
  page,
}) => {
  /**
   * The endpoint never selects those columns, which is where the guarantee
   * lives. This is the end-to-end confirmation that nothing downstream puts them
   * back -- and it is checked against a real adjustment reason if the seeded
   * data has one.
   */
  const anyReason = await withDb(
    async (c) =>
      (
        await c.query(
          `SELECT reason, actor_id, actor_role, correlation_id
             FROM ledger_entries
            WHERE reason IS NOT NULL OR actor_id IS NOT NULL
                  OR correlation_id IS NOT NULL
            LIMIT 1`,
        )
      ).rows[0],
  );

  await signInAsBorrower(page);
  await page.goto("/my-loan");
  await expect(page.getByTestId("account-activity").first()).toBeVisible({
    timeout: 20_000,
  });

  const body = (await page.locator("body").textContent()) ?? "";
  if (anyReason?.reason) expect(body).not.toContain(anyReason.reason);
  if (anyReason?.correlation_id) expect(body).not.toContain(anyReason.correlation_id);
  // And the implementation name for a trigger-captured write, which the seeded
  // database really contains.
  expect(body.toLowerCase()).not.toContain("legacy_direct_write");
  expect(body.toLowerCase()).not.toContain("legacy direct write");
});

test("an authenticated borrower is refused another borrower's activity", async ({
  page,
}) => {
  /**
   * Review of PR #87 (AA-TEST-001), and the finding was right: this used the
   * isolated `request` fixture with no session and accepted a 401, so an
   * ANONYMOUS refusal passed for an ownership check. The two are different
   * claims -- "you must log in" and "this is not your loan" -- and only the
   * second is what the gateway rule exists to enforce.
   *
   * Signed in as a real borrower now, sending that session's own token, against
   * a loan they demonstrably do not own.
   */
  await signInAsBorrower(page);

  // A loan that exists and belongs to someone else, so the refusal cannot be
  // "no such loan".
  const otherLoanId = await withDb(
    async (c) =>
      (
        await c.query(
          `SELECT l.id FROM loans l
            WHERE l.id <> $1 AND l.status = 'current'
            ORDER BY l.id LIMIT 1`,
          [SEEDED_BORROWER.loanId],
        )
      ).rows[0]?.id,
  );
  expect(otherLoanId, "no second serviced loan to test ownership against")
    .toBeTruthy();

  const outcome = await page.evaluate(async (loanId) => {
    const token = window.localStorage.getItem("meridian.token") ?? "";
    const res = await fetch(`http://localhost:8000/lss/loans/${loanId}/activity`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    return { status: res.status, sentToken: token.length > 0 };
  }, otherLoanId);

  // The session really was sent -- otherwise this is the anonymous test again
  // under a new name.
  expect(outcome.sentToken).toBe(true);
  expect(outcome.status).toBe(403);

  // And the borrower's OWN loan is readable with the same token, so the 403
  // above is about ownership rather than about the route being unreachable.
  const own = await page.evaluate(async (loanId) => {
    const token = window.localStorage.getItem("meridian.token") ?? "";
    const res = await fetch(`http://localhost:8000/lss/loans/${loanId}/activity`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    return res.status;
  }, SEEDED_BORROWER.loanId);
  expect(own).toBe(200);
});


test("activity re-reads when the page refreshes, without a reload", async ({
  page,
}) => {
  /**
   * Review of PR #87 (AA-FRESH-001). The panel fetched on mount only, so a
   * payment the user had just captured showed in payment history -- which the
   * page re-reads -- and not in activity beside it. Two panels on one screen
   * disagreeing about the same account is worse than either being absent,
   * because both look authoritative.
   *
   * Driven from the BORROWER page, because that is where the refresh path has a
   * button. The staff page bumps the same prop from
   * `refreshBalanceAndHistory()`, which only runs after a money action -- and
   * exercising it would mean capturing a real card payment into a shared demo
   * database on every run.
   *
   * Asserted with an intercepted response that CHANGES between the two reads. A
   * panel that never re-fetched would still be showing the first answer, so the
   * assertion cannot pass for the wrong reason.
   */
  let served = 0;
  await signInAsBorrower(page);
  await page.route("**/lss/loans/*/activity", (route) => {
    served += 1;
    const items =
      served <= 1
        ? []
        : [
            {
              id: "payment:9100",
              occurred_at: "2026-08-24T12:00:00+00:00",
              category: "payment",
              description: "Payment received",
              amount: -120,
              components: { principal: -120 },
              payment_id: 9100,
              provenance: "processor",
            },
          ];
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        loan_id: SEEDED_BORROWER.loanId,
        items,
        note: "Authoritative movements that changed this account, read from the immutable ledger.",
      }),
    });
  });

  await page.goto("/my-loan");
  const activity = page.getByTestId("account-activity").first();
  await expect(activity).toBeVisible({ timeout: 30_000 });
  // First read: nothing yet.
  await expect(activity.getByText(/No movements have been recorded/i)).toBeVisible();

  await page.getByRole("button", { name: /Refresh/i }).first().click();

  await expect(activity.locator("tbody tr")).toHaveCount(1, { timeout: 30_000 });
  await expect(activity.getByText(/payment 9100/)).toBeVisible();
  expect(served).toBeGreaterThan(1);
});


test("the amounts on screen are the server's, to the cent", async ({
  page,
  request,
}) => {
  /**
   * The strongest available statement that the browser does no accounting: read
   * the endpoint and the page, and require them to agree exactly.
   */
  const loanId = SEEDED_BORROWER.loanId;

  await signInAsStaff(page, "csr");
  await page.goto(`/servicing/${loanId}`);
  const activity = page.getByTestId("account-activity");
  await expect(activity).toBeVisible({ timeout: 20_000 });

  // Through the page's own session, so the gateway authorises it the same way.
  const fromApi = await page.evaluate(async (id) => {
    const res = await fetch(`http://localhost:8000/lss/loans/${id}/activity`, {
      headers: {
        // `meridian.token`, the key `lib/api.ts` actually writes. The first
        // version read "token", got null, and the request came back
        // unauthorised -- which made this test SKIP, and a skip reads exactly
        // like a pass in a run summary.
        Authorization: `Bearer ${window.localStorage.getItem("meridian.token") ?? ""}`,
      },
    });
    return res.ok ? await res.json() : null;
  }, loanId);
  expect(fromApi, "the page's own session could not read the endpoint").not.toBeNull();

  const onScreen = await activity.locator("tbody tr td.num").allTextContents();
  const serverAmounts = (fromApi.items as { amount: number }[]).map((i) =>
    Math.abs(i.amount),
  );

  expect(onScreen.length).toBe(serverAmounts.length);
  onScreen.forEach((text, index) => {
    expect(money(text)).toBeCloseTo(serverAmounts[index], 2);
  });
});
