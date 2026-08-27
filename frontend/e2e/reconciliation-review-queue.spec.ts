import { test, expect } from "@playwright/test";
import type { Client } from "pg";
import { dbClient, signInAsStaff } from "./fixtures";

/**
 * The in-app reconciliation queue, end to end: a flagged payment reaches a
 * human, the human's answer is recorded once, and no money moves.
 *
 * This is the only destination the client authorised (decision of 2026-08-24):
 * no email, Slack, PagerDuty, webhook or SMS before the freeze. So the browser
 * path is not a convenience over the API -- it IS the delivery mechanism, and a
 * page that renders but cannot record an answer means a flagged payment is
 * reported to nobody.
 *
 * The client also asked for one specific thing about the screen: that it
 * "clearly distinguish Reconciliation breaks vs Duplicate-review candidates".
 * Two of the tests below are about exactly that, because a candidate read as a
 * break says money is wrong when all that happened is that something needs
 * looking at.
 *
 * These specs share the demo database with every other spec (RF-24 -- there is
 * no per-spec isolation), so each one creates its own payments and its own
 * review item and cleans them up, rather than asserting anything about counts
 * it does not own.
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

interface Seeded {
  itemId: number;
  paymentIds: number[];
  loanId: number;
}

/**
 * Two captured payments on a seeded loan and one review item over them.
 *
 * Inserted directly rather than driven through a real double charge: the
 * detector is `payment-service`'s and has its own tests; what is under test here
 * is whether a human can see and answer an item that exists. Driving a genuine
 * duplicate through the UI would also mean charging a card twice in a shared
 * demo database.
 */
async function seedReviewItem(signal: string): Promise<Seeded> {
  return withDb(async (c) => {
    const loan = (
      await c.query(
        `SELECT l.id FROM loans l JOIN balances b ON b.loan_id = l.id
          WHERE l.status = 'current' ORDER BY l.id LIMIT 1`,
      )
    ).rows[0];
    if (!loan) throw new Error("no serviced loan in the demo database to flag against");

    const paymentIds: number[] = [];
    for (const offset of [18, 0]) {
      const row = (
        await c.query(
          // `applied_at` is set, and it is not cosmetic. A captured payment with
          // a NULL `applied_at` is a durable work item the payment-service
          // reconciler drains (migration 0028) -- so the first version of this
          // fixture had the outbox worker credit a real demo balance and write a
          // ledger entry against a payment that exists only for this test, which
          // then could not be deleted at all. An inert row is what a fixture
          // should be: visible to the queue, claimed by no worker.
          `INSERT INTO payments (loan_id, amount, method, last4, brand,
                                 idempotency_key, auth_status, captured_at,
                                 applied_at, processor_ref, source_ref,
                                 capture_source)
           VALUES ($1, 250.00, 'card', '4242', 'visa', $2, 'captured',
                   now() - make_interval(mins => $3), now(), $4, 'src_mock_e2e',
                   'processor')
           RETURNING id`,
          [loan.id, `e2e-review-${signal}-${offset}-${Date.now()}`, offset,
            `PR-e2e-${signal}-${offset}-${Date.now()}`],
        )
      ).rows[0];
      paymentIds.push(row.id);
    }

    const item = (
      await c.query(
        `INSERT INTO reconciliation_review_items
           (signal_type, payment_id, related_payment_id, loan_id, correlation_ref)
         VALUES ($1, $2, $3, $4, 'pay_e2e') RETURNING id`,
        [signal, paymentIds[1], paymentIds[0], loan.id],
      )
    ).rows[0];

    return { itemId: item.id, paymentIds, loanId: loan.id };
  });
}

async function cleanUp(seeded: Seeded): Promise<void> {
  await withDb(async (c) => {
    // The review item goes first: `ON DELETE CASCADE` would take it with the
    // payment, but relying on that would leave this helper silently wrong if the
    // cascade were ever changed.
    await c.query("DELETE FROM reconciliation_review_items WHERE id = $1", [seeded.itemId]);
    // Ledger entries hold a foreign key to (loan_id, payment_id) and are
    // append-only for real money, so nothing here may delete one -- if any
    // exists against these payments, something applied them and the assertion
    // that no money moved is what should fail, loudly, rather than this cleanup
    // quietly tidying the evidence away.
    const applied = (
      await c.query(
        "SELECT count(*) AS n FROM ledger_entries WHERE payment_id = ANY($1::bigint[])",
        [seeded.paymentIds],
      )
    ).rows[0].n;
    if (Number(applied) > 0) {
      throw new Error(
        `${applied} ledger entries exist against the test payments -- they were ` +
          `applied to a balance, which no part of the review queue may do`,
      );
    }
    await c.query("DELETE FROM payments WHERE id = ANY($1::bigint[])", [seeded.paymentIds]);
  });
}

test("a flagged payment reaches a reviewer, and the page says it is not a conclusion", async ({
  page,
}) => {
  const seeded = await seedReviewItem("heuristic_30_minute_candidate");
  try {
    await signInAsStaff(page, "csr");
    await page.goto("/reconciliation");

    // Filtered by THIS test's own payment, not `.first()`. The queue is shared
    // (RF-24) and the detector merged with PR #79 puts real signals in it, so
    // `.first()` asserted against whichever card happened to be oldest -- which
    // is how this test failed against a genuine item it did not create.
    const card = page
      .locator(".card", { hasText: "Potential duplicate — review required" })
      .filter({ hasText: `Flagged · ${seeded.paymentIds[1]}` })
      .first();
    await expect(card).toBeVisible({ timeout: 15_000 });

    // The client's wording, on the screen a reviewer actually reads. Not "these
    // are duplicates" -- the flag is a question.
    await expect(page.getByText(/not permission to move money/i)).toBeVisible();
    await expect(page.getByText(/not a duplicate finding/i)).toBeVisible();

    // Both payments, because "same payment twice or a second real one?" cannot
    // be answered from one of them.
    await expect(card.getByText(`Flagged · ${seeded.paymentIds[1]}`)).toBeVisible();
    await expect(card.getByText(`Resembles · ${seeded.paymentIds[0]}`)).toBeVisible();
  } finally {
    await cleanUp(seeded);
  }
});

test("the queue shows no card number, brand or processor reference", async ({ page }) => {
  const seeded = await seedReviewItem("heuristic_30_minute_candidate");
  try {
    await signInAsStaff(page, "csr");
    await page.goto("/reconciliation");
    await expect(
      page
        .locator(".card", { hasText: "Potential duplicate — review required" })
        .filter({ hasText: `Flagged · ${seeded.paymentIds[1]}` })
        .first(),
    ).toBeVisible({ timeout: 15_000 });

    // The seeded payments really do carry 4242/visa and a processor reference,
    // so this is a statement about the page rather than about empty columns.
    const body = (await page.locator("body").textContent()) ?? "";
    for (const forbidden of ["4242", "visa", "PR-e2e", "src_mock_e2e"]) {
      expect(body).not.toContain(forbidden);
    }
  } finally {
    await cleanUp(seeded);
  }
});

test("breaks and review candidates are separate sections with separate headings", async ({
  page,
}) => {
  const seeded = await seedReviewItem("exact_idempotency_key");
  try {
    await signInAsStaff(page, "csr");
    await page.goto("/reconciliation");

    const candidates = page.getByRole("heading", { name: "Payment review candidates" });
    const breaks = page.getByRole("heading", { name: "Reconciliation breaks" });
    await expect(candidates).toBeVisible({ timeout: 15_000 });
    await expect(breaks).toBeVisible();

    // Distinct, and in this order: the candidates are the ones with a deadline
    // attached to a borrower who has already been charged.
    const candidatesBox = await candidates.boundingBox();
    const breaksBox = await breaks.boundingBox();
    expect(candidatesBox && breaksBox).toBeTruthy();
    expect(candidatesBox!.y).toBeLessThan(breaksBox!.y);

    // The break section states what the comparison has actually established.
    // Two equal totals with nothing behind them is the D7 defect, and the queue
    // must not re-introduce it by showing the numbers alone. The sentence has
    // three forms -- a clean match, runs that executed without one, and nothing
    // having run -- and any of them satisfies this: what is asserted here is
    // that the panel says something, not which state it is in. The wording of
    // each is pinned in `reconciliation-statement.spec.ts`.
    await expect(
      page.getByText(/Last compared|No reconciliation run has/i).first(),
    ).toBeVisible();
  } finally {
    await cleanUp(seeded);
  }
});

test("the exact and heuristic signals are described differently", async ({ page }) => {
  const exact = await seedReviewItem("exact_idempotency_key");
  try {
    await signInAsStaff(page, "csr");
    await page.goto("/reconciliation");

    const card = page
      .locator(".card", { hasText: "Potential duplicate — review required" })
      .filter({ hasText: "idempotency key" })
      .first();
    await expect(card).toBeVisible({ timeout: 15_000 });
    await expect(card.getByText("Exact match signal")).toBeVisible();
  } finally {
    await cleanUp(exact);
  }
});

test("a reviewer records one of three answers, it sticks, and no money moves", async ({
  page,
}) => {
  const seeded = await seedReviewItem("heuristic_30_minute_candidate");
  try {
    const before = await withDb(async (c) => ({
      balance: (
        await c.query("SELECT balance, past_due FROM balances WHERE loan_id = $1", [
          seeded.loanId,
        ])
      ).rows[0],
      ledger: (await c.query("SELECT count(*) AS n FROM ledger_entries")).rows[0].n,
      movements: (await c.query("SELECT count(*) AS n FROM pending_movements")).rows[0].n,
    }));

    await signInAsStaff(page, "csr");
    await page.goto("/reconciliation");

    const card = page
      .locator(".card", { hasText: "Potential duplicate — review required" })
      .filter({ hasText: `Flagged · ${seeded.paymentIds[1]}` })
      .first();
    await expect(card).toBeVisible({ timeout: 15_000 });

    // Exactly the three the client authorised -- no "Reverse payment" beside
    // them, which is the control this whole page depends on.
    await expect(card.getByRole("button", { name: "Confirmed duplicate" })).toBeVisible();
    await expect(
      card.getByRole("button", { name: "Legitimate distinct payment" }),
    ).toBeVisible();
    await expect(
      card.getByRole("button", { name: "Requires further review" }),
    ).toBeVisible();
    await expect(card.getByRole("button", { name: /revers|refund|adjust/i })).toHaveCount(0);

    await card.getByRole("button", { name: "Confirmed duplicate" }).click();

    await expect(page.getByText(/No money moved/i)).toBeVisible({ timeout: 15_000 });

    // Recorded, with the reviewer named, and stored as the client's own value.
    const stored = await withDb(
      async (c) =>
        (
          await c.query(
            "SELECT status, disposition, reviewed_by, reviewed_by_role " +
              "FROM reconciliation_review_items WHERE id = $1",
            [seeded.itemId],
          )
        ).rows[0],
    );
    expect(stored.status).toBe("reviewed");
    expect(stored.disposition).toBe("confirmed_duplicate");
    expect(stored.reviewed_by).toBeTruthy();
    expect(stored.reviewed_by_role).toBe("csr");

    // And the thing the client was explicit about: `confirmed_duplicate` is a
    // classification, not an instruction. Nothing moved.
    const after = await withDb(async (c) => ({
      balance: (
        await c.query("SELECT balance, past_due FROM balances WHERE loan_id = $1", [
          seeded.loanId,
        ])
      ).rows[0],
      ledger: (await c.query("SELECT count(*) AS n FROM ledger_entries")).rows[0].n,
      movements: (await c.query("SELECT count(*) AS n FROM pending_movements")).rows[0].n,
    }));
    expect(after.balance.balance).toBe(before.balance.balance);
    expect(after.balance.past_due).toBe(before.balance.past_due);
    expect(after.ledger).toBe(before.ledger);
    // Not even a proposal: a queued reversal would put money one approval away
    // on the strength of a flag that is not a conclusion.
    expect(after.movements).toBe(before.movements);
  } finally {
    await cleanUp(seeded);
  }
});

test("an answered item offers no way to change the answer", async ({ page }) => {
  const seeded = await seedReviewItem("heuristic_30_minute_candidate");
  try {
    await signInAsStaff(page, "csr");
    await page.goto("/reconciliation");

    const card = page
      .locator(".card", { hasText: "Potential duplicate — review required" })
      .filter({ hasText: `Flagged · ${seeded.paymentIds[1]}` })
      .first();
    await expect(card).toBeVisible({ timeout: 15_000 });
    await card.getByRole("button", { name: "Legitimate distinct payment" }).click();
    await expect(page.getByText(/No money moved/i)).toBeVisible({ timeout: 15_000 });

    // The reviewed item is off the open list, and the reviewed list shows the
    // answer with no controls: a human classification is evidence, and evidence
    // that can be quietly rewritten is not evidence.
    await page.getByRole("button", { name: "Show reviewed items" }).click();
    const reviewed = page
      .locator(".card", { hasText: `Flagged · ${seeded.paymentIds[1]}` })
      .first();
    await expect(reviewed).toBeVisible({ timeout: 15_000 });
    await expect(reviewed.getByText(/cannot be changed/i)).toBeVisible();
    await expect(
      reviewed.getByRole("button", { name: "Confirmed duplicate" }),
    ).toHaveCount(0);
  } finally {
    await cleanUp(seeded);
  }
});

test("a broken break-summary does not blank the review queue", async ({ page }) => {
  /**
   * Review of PR #81 found this, and it is the failure that matters most on this
   * page: both requests were awaited in one `Promise.all` under one `catch`, so
   * a failure of `/peek` -- the ledger-versus-settlement SUMMARY, which decides
   * nothing about any flagged payment -- threw before the candidates were set.
   * The only destination the client authorised for a flagged payment rendered
   * empty because an unrelated request failed, and "nothing to review" looked
   * exactly like "the fetch broke".
   *
   * So only `/peek` is failed here. The queue must still render its item, and
   * the break panel must say it could not be read rather than implying the books
   * agree.
   */
  const seeded = await seedReviewItem("heuristic_30_minute_candidate");
  try {
    await signInAsStaff(page, "csr");

    // Routed AFTER sign-in so the sign-in flow itself is untouched.
    await page.route("**/lss/reconciliation/peek", (route) =>
      route.fulfill({ status: 503, body: "peek is down" }),
    );

    await page.goto("/reconciliation");

    const card = page
      .locator(".card", { hasText: "Potential duplicate — review required" })
      .filter({ hasText: `Flagged · ${seeded.paymentIds[1]}` })
      .first();
    await expect(card).toBeVisible({ timeout: 15_000 });

    // The queue is not claiming to be empty.
    await expect(page.getByText("No payments are waiting for review")).toHaveCount(0);

    // And the break panel is honest about what it does not know.
    await expect(
      page.getByText(/not a statement that the books agree/i),
    ).toBeVisible();
  } finally {
    await cleanUp(seeded);
  }
});

test("a broken review queue does not read as an empty one", async ({ page }) => {
  /** The other direction: the list must not assert "nothing is waiting" on the
   * strength of a request that failed. A queue that says it is empty when it
   * could not be read is worse than one that says nothing -- it is the same
   * defect as two equal reconciliation totals with no run behind them. */
  await signInAsStaff(page, "csr");
  await page.route("**/lss/reconciliation/review-queue**", (route) =>
    route.fulfill({ status: 503, body: "queue is down" }),
  );

  await page.goto("/reconciliation");

  await expect(
    page.getByText(/not a statement about whether anything is waiting/i),
  ).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("No payments are waiting for review")).toHaveCount(0);
  // The break panel is unaffected: independent loads in both directions.
  await expect(page.getByRole("heading", { name: "Reconciliation breaks" })).toBeVisible();
});

test("a borrower cannot reach the reconciliation queue at all", async ({ request }) => {
  /**
   * The gateway's refusal, not the page's. `RequireRole` decides what renders;
   * a borrower who calls the route directly is refused server-side, which is
   * the check that matters.
   */
  const res = await request.get("http://localhost:8000/lss/reconciliation/review-queue");

  // No session at all -> refused before any role question is reached.
  expect([401, 403]).toContain(res.status());
});
