import { test, expect } from "@playwright/test";
import type { Page } from "@playwright/test";
import type { Client } from "pg";
import {
  createBorrowerLoan,
  dbClient,
  retireBorrowerLoans,
  signInAsBorrower,
} from "./fixtures";

/**
 * A payment tells the borrower which of three things happened, and the receipt
 * describes the payment they actually made.
 *
 * `payment-service` returns HTTP 200 with a `status` of `captured`, `pending` or
 * `failed` (`PaymentOut.status`), and they mean different things. The page used
 * to branch on `captured` or else-pending, so a processor **decline** was shown
 * as *"pending — click again to retry"* with the idempotency key left in place.
 * That is worse than a wrong label: `payments.py` is explicit that a declined
 * key stays declined ("a borrower who wants to actually retry the charge needs a
 * new idempotency_key"), so the screen invited a retry that could never succeed
 * however many times it was clicked.
 *
 * **The three states are driven by intercepting the charge response**, not by
 * persuading a mock processor to decline. What is under test is the page's
 * handling of a contract the server already has, and a real decline would need
 * processor behaviour this environment cannot produce on demand. The contract
 * itself — that a decline yields `failed` and applies nothing — is asserted in
 * `services/payment-service/tests/`, where it belongs.
 *
 * Grouped by page load: the gateway rate-limits 120 requests per 60 seconds, and
 * a sign-in plus page load per assertion exceeded it.
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

/** A loan this file owns. See `payment-allocation.spec.ts` and RF-30: these
 * specs pay through the real UI, and a payment permanently reduces the balance,
 * so sharing the seeded borrower loan drained it at about 1030 per full run. */
const FIXTURE_LABEL = "payment-receipt";

let LOAN = 0;

test.beforeAll(async () => {
  LOAN = await withDb(async (c) => (await createBorrowerLoan(c, FIXTURE_LABEL)).loanId);
});

test.afterAll(async () => {
  await withDb((c) => retireBorrowerLoans(c, FIXTURE_LABEL));
});

async function openAccount(page: Page): Promise<void> {
  await page.goto(`/servicing/${LOAN}`);
  await expect(page.getByRole("heading", { name: /Make a payment/i })).toBeVisible({
    timeout: 60_000,
  });
  await expect(page.getByTestId("payment-context")).toBeVisible({ timeout: 60_000 });
}

/** Intercept the charge and answer with one chosen outcome. */
async function chargeReturns(
  page: Page,
  body: { status: string; payment_id: number | null; applied_amount?: number },
): Promise<() => number> {
  let calls = 0;
  await page.route("**/payments", (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    calls += 1;
    return route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        loan_id: LOAN,
        applied_amount: body.applied_amount ?? 0,
        ...body,
      }),
    });
  });
  return () => calls;
}

async function pay(page: Page, amount: string): Promise<void> {
  await page.getByLabel("Amount (USD)", { exact: true }).fill(amount);
  await page.getByRole("button", { name: /Pay with card on file/ }).click();
}

/** The idempotency key currently on the page's next request. */
async function keyOnNextRequest(page: Page): Promise<string> {
  const [request] = await Promise.all([
    page.waitForRequest(
      (r) => r.url().endsWith("/payments") && r.method() === "POST",
    ),
    page.getByRole("button", { name: /Pay with card on file/ }).click(),
  ]);
  return JSON.parse(request.postData() ?? "{}").idempotency_key;
}

test("the payment form states the contract and the account context", async ({
  page,
}) => {
  const balances = await withDb(
    async (c) =>
      (
        await c.query(
          `SELECT balance::float8 AS balance, COALESCE(past_due, 0)::float8 AS past_due
             FROM balances WHERE loan_id = $1`,
          [LOAN],
        )
      ).rows[0],
  );

  await signInAsBorrower(page);
  await openAccount(page);

  // The false promise is gone. It said payments post immediately, which the
  // three-state contract contradicts outright.
  await expect(page.getByText(/post immediately/i)).toHaveCount(0);
  await expect(
    page.getByText(/applied once the processor capture and Meridian/i),
  ).toBeVisible();

  // Authoritative context, from the server.
  const rows = page.getByTestId("payment-context").locator(".dl-row");
  await expect(rows.nth(0).locator("dt")).toHaveText(/Current principal balance/i);
  await expect(rows.nth(0).locator("dd")).toHaveText(
    balances.balance.toLocaleString("en-US", {
      style: "currency",
      currency: "USD",
      minimumFractionDigits: 2,
    }),
  );
  await expect(rows.nth(1).locator("dt")).toHaveText(/Outstanding fees/i);

  // The waterfall is explained and no dollar split is predicted.
  await expect(page.getByText(/fees.*then.*interest.*then.*principal/i)).toBeVisible();
});

test("a captured payment shows a receipt for that exact payment", async ({
  page,
}) => {
  /**
   * The identity requirement. TWO payments of the same amount are seeded into
   * the response path, and the receipt must describe the one the charge returned
   * -- so a "find by amount" or "take the newest row" implementation fails here.
   */
  const paid = 137.11;
  const decoyId = 900001;
  const realId = 900002;

  await signInAsBorrower(page);

  // Payment history carries two rows with the SAME amount. The decoy is newest,
  // so "latest row" picks the wrong one; its split differs, so the receipt shows
  // visibly wrong numbers if the match is not by id.
  await page.route(`**/lss/loans/${LOAN}/payments`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        loan_id: LOAN,
        items: [
          {
            id: decoyId,
            amount: paid,
            method: "card",
            created_at: "2026-08-25T12:00:00+00:00",
            applied_to_fees: 137.11,
            applied_to_interest: 0,
            applied_to_principal: 0,
          },
          {
            id: realId,
            amount: paid,
            method: "card",
            created_at: "2026-08-25T11:00:00+00:00",
            applied_to_fees: 25,
            applied_to_interest: 75,
            applied_to_principal: 37.11,
          },
        ],
      }),
    }),
  );
  const charges = await chargeReturns(page, {
    status: "captured",
    payment_id: realId,
    applied_amount: paid,
  });

  await openAccount(page);
  await pay(page, String(paid));

  const receipt = page.getByTestId("payment-posted");
  await expect(receipt).toBeVisible({ timeout: 30_000 });
  await expect(receipt).toContainText(`payment ${realId}`);

  // The split of the payment the server named -- 25 / 75 / 37.11 -- not the
  // decoy's 137.11 / 0 / 0.
  const split = page.getByTestId("payment-receipt-split").locator(".dl-row");
  await expect(split.nth(0).locator("dd")).toHaveText("$25.00");
  await expect(split.nth(1).locator("dd")).toHaveText("$75.00");
  await expect(split.nth(2).locator("dd")).toHaveText("$37.11");

  // The refreshed principal, and it is not the browser's arithmetic.
  await expect(page.getByTestId("payment-receipt-principal")).toBeVisible();
  expect(charges()).toBe(1);

  // Nothing anywhere claims a payoff.
  await expect(page.getByText(/payoff/i)).toHaveCount(0);
});

test("a pending payment claims nothing, and keeps the same idempotency key", async ({
  page,
}) => {
  await signInAsBorrower(page);
  await chargeReturns(page, { status: "pending", payment_id: 900010 });

  await openAccount(page);
  await pay(page, "50.00");

  const pending = page.getByTestId("payment-pending");
  await expect(pending).toBeVisible({ timeout: 30_000 });
  await expect(pending).toContainText(/not yet confirmed/i);
  await expect(pending).toContainText(/will not charge you twice/i);
  // The heading has to say WHICH half is outstanding. Two things can be
  // unresolved after a card is presented -- the charge, and its application to
  // the loan -- and only the second is. A reader who took "pending" to mean the
  // card had not gone through would try again on a new key and authorise a
  // second charge.
  await expect(pending).toContainText(/captured/i);
  await expect(pending).toContainText(/allocation pending/i);
  // No estimated split may appear while the allocation is unknown: null is not
  // zero, and three zeros here would be a guess wearing the shape of a receipt.
  await expect(page.getByTestId("payment-receipt-split")).toHaveCount(0);

  // No receipt, no posted claim, no split.
  await expect(page.getByTestId("payment-posted")).toHaveCount(0);
  await expect(page.getByTestId("payment-receipt-split")).toHaveCount(0);
  await expect(page.getByText(/Payment posted/i)).toHaveCount(0);

  // The key is UNCHANGED, so retrying reconciles the same payment rather than
  // authorising a second charge. Read off the next request the page makes.
  const keyAfterPending = await keyOnNextRequest(page);
  const keyAfterRetry = await keyOnNextRequest(page);
  expect(keyAfterPending).toBe(keyAfterRetry);
});

test("a declined payment says declined, and frees the key for a new attempt", async ({
  page,
}) => {
  /**
   * The defect this spec exists for. A decline used to render as pending with
   * the key retained, so the retry the screen suggested replayed a refusal.
   */
  await signInAsBorrower(page);
  await chargeReturns(page, { status: "failed", payment_id: 900020 });

  await openAccount(page);
  await pay(page, "75.00");

  const declined = page.getByTestId("payment-declined");
  await expect(declined).toBeVisible({ timeout: 30_000 });
  await expect(declined).toContainText(/no payment was applied/i);

  // Not pending, not posted.
  await expect(page.getByTestId("payment-pending")).toHaveCount(0);
  await expect(page.getByTestId("payment-posted")).toHaveCount(0);
  await expect(page.getByText(/pending/i)).toHaveCount(0);

  // A NEW key, because a declined key stays declined -- the next attempt has to
  // be a genuinely new payment or it can never succeed.
  const firstRetryKey = await keyOnNextRequest(page);
  const secondRetryKey = await keyOnNextRequest(page);
  expect(firstRetryKey).not.toBe(secondRetryKey);
});

test("a captured payment with no allocation evidence says so rather than showing zeros", async ({
  page,
}) => {
  /**
   * `null` means "no ledger evidence"; `0.00` means "this component received
   * nothing". Rendering the first as the second would state a split nobody
   * recorded -- the distinction `lib/allocation.ts` exists to keep.
   */
  const id = 900030;
  await signInAsBorrower(page);
  await page.route(`**/lss/loans/${LOAN}/payments`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        loan_id: LOAN,
        items: [
          {
            id,
            amount: 60,
            method: "card",
            created_at: "2026-08-25T12:00:00+00:00",
            applied_to_fees: null,
            applied_to_interest: null,
            applied_to_principal: null,
          },
        ],
      }),
    }),
  );
  await chargeReturns(page, { status: "captured", payment_id: id, applied_amount: 60 });

  await openAccount(page);
  await pay(page, "60.00");

  await expect(page.getByTestId("payment-posted")).toBeVisible({ timeout: 30_000 });
  await expect(page.getByTestId("payment-receipt-unavailable")).toBeVisible();
  await expect(page.getByTestId("payment-receipt-split")).toHaveCount(0);
  // And it does not invent three zeros.
  await expect(page.getByTestId("payment-posted")).not.toContainText("$0.00");
});
