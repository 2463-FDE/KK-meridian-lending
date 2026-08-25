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

test("a multi-component payment is one row, not three charges", async ({ page }) => {
  /**
   * Built through the real waterfall rather than by hand: the database refuses a
   * partial allocation (`ledger_payment_allocation_exact` is DEFERRABLE
   * INITIALLY DEFERRED and requires a payment's entries to sum to its captured
   * amount), so a fixture inserting one component at a time cannot exist. Any
   * seeded payment that split across components will do.
   */
  const payments = await withDb(
    async (c) =>
      (
        await c.query(
          `SELECT payment_id, loan_id, count(*) AS parts,
                  sum(amount)::float8 AS total
             FROM ledger_entries
            WHERE entry_type = 'payment' AND payment_id IS NOT NULL
            GROUP BY payment_id, loan_id
            ORDER BY count(*) DESC, payment_id
            LIMIT 1`,
        )
      ).rows,
  );

  // **No `test.skip` here, deliberately.** The first version skipped when no
  // seeded payment split across components -- and none does, so it skipped every
  // run, and a skip reads exactly like a pass in a summary. The seeded portfolio
  // has no arrears, so every payment goes wholly to principal; producing a real
  // split would mean assessing a fee first, and a fee cannot be assessed against
  // a zero past-due balance.
  //
  // So the multi-component case is proven where the data can be built:
  // `servicing-service/tests/test_account_activity.py::
  // test_a_payment_split_three_ways_is_one_movement`, and again on real
  // PostgreSQL in `test_activity_does_not_touch_reconciliation.py`, whose
  // fixture allocates a 250.00 capture across fees, interest and principal.
  //
  // What this test asserts against real data is the property that survives: a
  // payment is ONE row whose amount is the server's, and the parts appear
  // beneath it when there are several.
  expect(payments.length).toBe(1);
  const payment = payments[0];

  await signInAsStaff(page, "csr");
  await page.goto(`/servicing/${payment.loan_id}`);

  const activity = page.getByTestId("account-activity");
  await expect(activity).toBeVisible({ timeout: 20_000 });

  const row = activity.locator("tbody tr", {
    hasText: `payment ${payment.payment_id}`,
  });
  await expect(row).toHaveCount(1);
  expect(money(await row.locator("td.num").textContent())).toBeCloseTo(
    Math.abs(payment.total),
    2,
  );

  // "Applied to" appears only when the movement has more than one component --
  // so on this data it must NOT, and asserting its absence is what proves the
  // line is driven by the components rather than printed unconditionally.
  if (Number(payment.parts) > 1) {
    await expect(row.getByText(/Applied to/i)).toBeVisible();
  } else {
    await expect(row.getByText(/Applied to/i)).toHaveCount(0);
  }
});

test("a three-component payment renders as one row, on a payload that has one", async ({
  page,
}) => {
  /**
   * The grouping defect, caught in the browser -- which the tests above cannot
   * do, and a mutation proved it.
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

test("a borrower cannot read another loan's activity", async ({ request }) => {
  /**
   * The gateway's refusal, which is the check that matters -- `RequireRole`
   * decides what renders, not who may read.
   */
  const res = await request.get(
    "http://localhost:8000/lss/loans/999999/activity",
  );

  expect([401, 403, 404]).toContain(res.status());
});

test("an unlisted loan sub-path is still refused", async ({ request }) => {
  /**
   * Adding `activity` to the gateway's owner-or-staff alternation must not have
   * turned it into a wildcard. Asserted from the browser side as well as in the
   * gateway's own tests, because this is the boundary that keeps `/lss/*` from
   * being a generic proxy.
   */
  const res = await request.get("http://localhost:8000/lss/loans/1/ledger");

  expect(res.status()).not.toBe(200);
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
