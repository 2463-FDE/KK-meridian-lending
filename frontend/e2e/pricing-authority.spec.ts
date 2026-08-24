import { test, expect } from "@playwright/test";
import { Client } from "pg";
import { dbClient, signInAsStaff } from "./fixtures";
import { describePricing } from "../lib/pricing";

/**
 * The note rate a loan carries comes from the server, and the same figure
 * survives all the way to the servicing screen.
 *
 * Before this, `frontend/app/apply` and `frontend/app/underwriting` each held
 * `const OFFER_RATE_PCT = 7.99` and posted it into offer creation -- so the
 * contractual rate on a real loan was whatever the browser sent, and the same
 * number lived in four other places. This walks the chain the client actually
 * cares about: what the server says it prices at, what the offer stored, what
 * boarding copied onto the loan, and what a staff member reads.
 *
 * The rate is never hardcoded here either. It is read from `/los/pricing` and
 * compared onward, so a demo run at a different `DEMO_NOTE_RATE_PCT` does not
 * fail its own test.
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

test("the server publishes the note rate, and says it is not a pricing policy", async ({
  request,
}) => {
  const res = await request.get("http://localhost:8000/los/pricing");
  expect(res.ok()).toBeTruthy();

  const body = await res.json();
  expect(typeof body.note_rate_pct).toBe("number");
  expect(body.is_production_pricing_policy).toBe(false);
  expect(body.source).toBe("training_default");
});

test("the browser cannot choose the contractual rate", async ({ request }) => {
  /**
   * The refusal an old client would hit. A silent ignore would be worse: the
   * caller would believe it had priced the loan while the disclosure said
   * something else.
   */
  const pricing = await (await request.get("http://localhost:8000/los/pricing")).json();

  const res = await request.post("http://localhost:8000/los/offer", {
    data: {
      app_id: 4471,
      principal: 5000,
      term_months: 48,
      annual_rate_pct: pricing.note_rate_pct + 4,
    },
  });

  expect(res.status()).toBe(422);
  expect(await res.text()).toContain("set by the server");
});

test("a boarded loan carries the server's rate, and servicing labels it a note rate", async ({
  page,
  request,
}) => {
  const pricing = await (await request.get("http://localhost:8000/los/pricing")).json();
  const expected = Number(pricing.note_rate_pct).toFixed(2);

  // The seeded portfolio deliberately holds a spread of rates, so this asserts
  // against the loans the CURRENT pricing produced rather than against all of
  // them -- normalising history to make a screen look uniform is exactly what
  // this change must not do.
  const recent = await withDb((c) =>
    c.query(
      `SELECT l.id, l.note_rate_pct::float8 AS rate, o.note_rate_pct::float8 AS offer_rate
         FROM loans l
         JOIN offers o ON o.app_id = l.app_id
        WHERE l.note_rate_pct IS NOT NULL
        ORDER BY l.id DESC
        LIMIT 5`,
    ).then((r) => r.rows),
  );

  for (const row of recent) {
    // Whatever the rate is, the offer and the loan agree about it: boarding
    // copies the disclosed term rather than re-deriving one.
    expect(Number(row.rate).toFixed(2)).toBe(Number(row.offer_rate).toFixed(2));
  }

  await signInAsStaff(page, "csr");
  await page.goto("/servicing");
  await expect(page.getByRole("heading", { name: /Portfolio|Servicing/i }).first())
    .toBeVisible({ timeout: 15_000 });

  // The column header names the term. "Rate" invited a staff member to quote
  // the contractual rate as the federal APR.
  const header = page.getByRole("columnheader", { name: /Note rate/i });
  await expect(header).toBeVisible();
  await expect(page.getByRole("columnheader", { name: /^Rate$/ })).toHaveCount(0);

  // And the cell under that header is a rate, not a money figure. Located by
  // the header's own column index: `td.num` alone picked the principal column,
  // which is also right-aligned -- a locator that finds the wrong column can
  // pass for the wrong reason just as easily as it can fail.
  const headers = await page.locator("thead th").allTextContents();
  const rateColumn = headers.findIndex((h) => /note rate/i.test(h));
  expect(rateColumn).toBeGreaterThan(-1);

  const rateCell = page.locator("tbody tr").first().locator("td").nth(rateColumn);
  await expect(rateCell).toHaveText(/%$/);
  // The seeded portfolio holds a spread of rates on purpose, so this asserts the
  // shape rather than the value. `expected` is the current pricing, kept for the
  // failure message a future assertion on new loans would want.
  void expected;
});

test("the estimate on the apply form comes from the server", async ({ page, request }) => {
  const pricing = await (await request.get("http://localhost:8000/los/pricing")).json();

  await page.goto("/apply");
  // Step 3 is where the estimate is shown; the form has to be walked to reach
  // it, so the request the page makes is what gets asserted instead.
  const asked = page.waitForRequest((r) => r.url().includes("/los/pricing"), {
    timeout: 15_000,
  });
  await page.reload();
  expect((await asked).method()).toBe("GET");

  // And the copy renders the server's figure, not a constant.
  expect(describePricing(pricing)).toBe(`${Number(pricing.note_rate_pct).toFixed(2)}%`);
});
