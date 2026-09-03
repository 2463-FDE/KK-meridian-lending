import { test, expect } from "@playwright/test";
import { HandleStore, sourceHandleFor, tokenizeCard } from "../lib/tokenize";
import {
  createBorrowerIdentity,
  dbClient,
  retireBorrowerIdentity,
  signInAsBorrower,
} from "./fixtures";
import type { Client } from "pg";

/**
 * The mock tokenizer's funding-source handle: stable, unique, and not the card.
 *
 * Why it exists: the client's decision of 2026-08-24 flags a payment for human
 * review only when loan, amount, payment SOURCE and channel all match inside 30
 * minutes. Nothing in this system could prove "same source" -- `processor_token`
 * is per capture and never persisted, `last4`/`brand` are instrument content and
 * not unique, and `capture_source` describes which writer produced the row. So
 * the mock supplies what a real processor SDK would: an opaque handle for the
 * instrument behind the payment.
 *
 * These run against the shipped function with an injected store, rather than
 * reproducing its logic in a page -- a test that rewrites the code it checks
 * proves only that the same lines can be typed twice. The browser case at the
 * end asserts the wiring, which is the part only a browser can show.
 *
 * Synthetic card numbers only: the published test PANs that appear in every
 * processor's documentation.
 */

const VISA = "4111111111111111";
const MASTERCARD = "5500000000000004";

/** A session store, as a Map. Same contract the browser's sessionStorage has. */
function memoryStore(): HandleStore {
  const map = new Map<string, string>();
  return {
    getItem: (key) => (map.has(key) ? (map.get(key) as string) : null),
    setItem: (key, value) => {
      map.set(key, value);
    },
  };
}

test("the same card yields the same handle within a session", () => {
  const store = memoryStore();

  const first = sourceHandleFor(VISA, store);
  const second = sourceHandleFor(VISA, store);

  expect(first).toBe(second);
  expect(first).toMatch(/^src_mock_[0-9a-f-]{36}$/);
});

test("two different cards yield different handles", () => {
  const store = memoryStore();

  expect(sourceHandleFor(VISA, store)).not.toBe(sourceHandleFor(MASTERCARD, store));
});

test("two cards sharing their last four still get different handles", () => {
  /**
   * The defect review caught in the first version: the storage key was
   * `last4 + length`, so every 16-digit card ending 1111 shared one handle. The
   * backend then saw same loan, same amount, same channel, same source and filed
   * a duplicate-review candidate for two genuinely different instruments --
   * exactly the false positive the source factor exists to prevent, arriving
   * through the mock rather than through the rule.
   */
  const store = memoryStore();
  const sameTail = ["4111111111111111", "4222222222221111"];

  const [a, b] = sameTail.map((pan) => sourceHandleFor(pan, store));

  expect(sameTail[0].slice(-4)).toBe(sameTail[1].slice(-4));  // the premise
  expect(a).not.toBe(b);
});

test("cards differing only in one middle digit get different handles", () => {
  const store = memoryStore();

  expect(sourceHandleFor("4111111111111111", store))
    .not.toBe(sourceHandleFor("4111111111112111", store));
});

test("the handle contains no part of the card number", () => {
  const handle = sourceHandleFor(VISA, memoryStore());

  // Not the PAN, not its last four, not its BIN -- and not a hash of any of
  // them. A hashed PAN is still PAN-derived data, and the point of this handle
  // is that the database learns nothing about the instrument from it.
  expect(handle).not.toContain(VISA);
  expect(handle).not.toContain(VISA.slice(-4));
  expect(handle).not.toContain(VISA.slice(0, 6));
});

test("a fresh session mints a new handle, which is the limitation not a bug", () => {
  /**
   * `sessionStorage` is per tab and per browser, so "same source" is provable
   * within a session only. The backend treats an unmatched handle as "cannot
   * prove same source" and emits no heuristic signal -- fail-closed toward NOT
   * flagging, which is the safe direction for a control whose false positives
   * land on a borrower's legitimate second payment.
   */
  expect(sourceHandleFor(VISA, memoryStore()))
    .not.toBe(sourceHandleFor(VISA, memoryStore()));
});

test("no store at all still yields a usable handle", () => {
  /** Private mode, or a browser with site data blocked. */
  expect(sourceHandleFor(VISA, null)).toMatch(/^src_mock_/);
});

test("a store that throws does not break tokenization", () => {
  const hostile: HandleStore = {
    getItem: () => {
      throw new Error("site data blocked");
    },
    setItem: () => {
      throw new Error("site data blocked");
    },
  };

  expect(sourceHandleFor(VISA, hostile)).toMatch(/^src_mock_/);
});

test("tokenizing returns a handle alongside the token, and they differ", () => {
  const token = tokenizeCard(VISA, "123");

  expect(token.source_ref).toMatch(/^src_mock_/);
  expect(token.processor_token).toMatch(/^tok_mock_/);
  // One identifies this capture, the other the instrument behind it. Collapsing
  // them would make every capture look like a new source.
  expect(token.source_ref).not.toBe(token.processor_token);
  // And neither carries the card.
  expect(token.processor_token).not.toContain(VISA);
  expect(token.source_ref).not.toContain(VISA);
});

/**
 * A loan this file owns, because the browser case below really pays (RF-31).
 *
 * It used to sign in as the seeded borrower inline and navigate to a hardcoded
 * `/servicing/4471`, then pay 59.00 with `route.continue()` -- so the payment
 * actually posted and permanently reduced that loan's balance. Measured: one
 * run took `4471` from `11487.01` to `11453.01`.
 *
 * That is the RF-30 defect a third time. RF-30 closed the two specs its sweep
 * found, and the sweep looked for `SEEDED_BORROWER`; this file names the loan id
 * as a bare literal and signs in with its own inline form-filling, so neither
 * pattern matched it. Recorded as RF-31 rather than folded silently into RF-30,
 * because RF-30's row claimed the borrower-payment drains were closed and that
 * claim was wrong.
 *
 * What this case actually needs is a borrower who owns a loan with a payment
 * form -- it asserts the OUTGOING REQUEST BODY, never the resulting balance --
 * so a synthetic identity serves it exactly as well as the seeded one, and
 * consumes nothing.
 *
 * Switching to `signInAsBorrower` is part of the fix rather than tidying: the
 * inline sign-in skipped `signInAndProveIt`, so this case also never got the
 * session proof every other spec has.
 */
const FIXTURE_LABEL = "payment-source-handle";

let BORROWER = "";
let LOAN = 0;

async function withDb<T>(fn: (c: Client) => Promise<T>): Promise<T> {
  const client = dbClient();
  await client.connect();
  try {
    return await fn(client);
  } finally {
    await client.end();
  }
}

test.beforeAll(async () => {
  const who = await withDb((c) => createBorrowerIdentity(c, FIXTURE_LABEL));
  BORROWER = who.username;
  LOAN = who.loanId;
});

test.afterAll(async () => {
  await withDb((c) => retireBorrowerIdentity(c, FIXTURE_LABEL));
});

test("the payment form sends the handle it was given", async ({ page }) => {
  /** The wiring, asserted on the outgoing request rather than on the source. */
  await signInAsBorrower(page, BORROWER);

  await page.goto(`/servicing/${LOAN}`);
  await expect(page.getByRole("heading", { name: /Payment history/i })).toBeVisible({
    timeout: 15_000,
  });

  const bodies: { source_ref?: string; processor_token?: string }[] = [];
  await page.route("**/payments", async (route) => {
    const post = route.request().postDataJSON();
    if (post) bodies.push(post);
    await route.continue();
  });

  await page.getByLabel("Amount (USD)", { exact: true }).fill("59.00");
  await page.getByRole("button", { name: /Pay with card on file/ }).click();
  await expect(page.locator(".alert-success, .alert-error")).toBeVisible({
    timeout: 20_000,
  });

  expect(bodies.length).toBeGreaterThan(0);
  expect(bodies[0].source_ref).toMatch(/^src_mock_/);
  expect(bodies[0].source_ref).not.toBe(bodies[0].processor_token);
});
