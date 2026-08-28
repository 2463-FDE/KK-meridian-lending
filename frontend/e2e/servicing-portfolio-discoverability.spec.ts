import { test, expect } from "@playwright/test";
import { dbClient, signInAsStaff } from "./fixtures";

/**
 * A loan you can reach must be a loan you can find.
 *
 * Verified before this change: an application accepted through the real flow
 * boarded correctly -- status funded, offer accepted, loan row, balance row, and
 * `/servicing/{id}` rendering the account -- and was still unfindable from the
 * servicing portfolio. Loans are ordered by `id`, ids ascend at boarding, the
 * page holds 25, so the new loan was rank 192 of 192: page 8. The toolbar's
 * search box filtered only the rows already fetched, so typing the id on page 1
 * returned "No loans match your filters."
 *
 * Nothing about boarding was wrong. The list was.
 *
 * These cases pin the two properties that fix it, from the operator's side:
 * the newest loan is on the first page by default, and an id is looked up across
 * the whole portfolio rather than the page in hand. The SQL-level facts --
 * direction, filters reaching the count, the enum being validated -- are pinned
 * in `services/servicing-service/tests/test_loan_list_ordering_and_lookup.py`.
 *
 * This spec only READS the portfolio. It boards nothing and mutates nothing, so
 * it does not consume a loan (RF-27) and can be re-run against one database.
 */

async function withDb<T>(fn: (c: import("pg").Client) => Promise<T>): Promise<T> {
  const client = dbClient();
  await client.connect();
  try {
    return await fn(client);
  } finally {
    await client.end();
  }
}

/** The extremes of the serviced portfolio, read from the database. */
async function idBounds(): Promise<{ newest: number; oldest: number; total: number }> {
  return withDb(async (c) => {
    const r = await c.query(
      "SELECT max(id)::int AS newest, min(id)::int AS oldest, count(*)::int AS total FROM loans",
    );
    return r.rows[0];
  });
}

const rowIds = async (page: import("@playwright/test").Page) =>
  (await page.locator("tbody tr td:first-child").allTextContents())
    .map((t) => Number(t.replace(/\D/g, "")))
    .filter((n) => Number.isFinite(n) && n > 0);

async function openPortfolio(page: import("@playwright/test").Page) {
  await signInAsStaff(page, "csr");
  await page.goto("/servicing");
  await expect(page.locator("#loan-id-search")).toBeVisible({ timeout: 30_000 });
  // Wait for real rows, not the "Loading loans..." placeholder -- that is also a
  // `tbody tr`, so waiting on the row alone reads an empty id list and fails for
  // the wrong reason.
  await expect
    .poll(async () => (await rowIds(page)).length, { timeout: 30_000 })
    .toBeGreaterThan(0);
}

test("the newest loan is on the first page without touching a control", async ({ page }) => {
  // The defect in one assertion: this is what an operator sees after boarding.
  const { newest } = await idBounds();
  await openPortfolio(page);

  const ids = await rowIds(page);
  expect(ids.length).toBeGreaterThan(0);
  expect(ids[0]).toBe(newest);
  expect(ids).toEqual([...ids].sort((a, b) => b - a));
});

test("oldest first shows the lowest ids, and switching back restores newest", async ({
  page,
}) => {
  const { newest, oldest } = await idBounds();
  await openPortfolio(page);

  await page.locator("#loan-order").selectOption("oldest");
  await expect
    .poll(async () => (await rowIds(page))[0], { timeout: 20_000 })
    .toBe(oldest);
  expect(await rowIds(page)).toEqual([...(await rowIds(page))].sort((a, b) => a - b));

  await page.locator("#loan-order").selectOption("newest");
  await expect
    .poll(async () => (await rowIds(page))[0], { timeout: 20_000 })
    .toBe(newest);
});

test("a high loan id is found from page 1, where the old search could not reach", async ({
  page,
}) => {
  // Searched from the DEFAULT page state, which is exactly where the old
  // client-side filter failed: the id was on the last page, so it was not among
  // the rows the filter could see.
  const { newest } = await idBounds();
  await openPortfolio(page);

  // Put the id off-page first, so the search has to reach past what is loaded.
  await page.locator("#loan-order").selectOption("oldest");
  await expect.poll(async () => (await rowIds(page)).includes(newest), { timeout: 20_000 })
    .toBe(false);

  await page.locator("#loan-id-search").fill(String(newest));
  await page.getByRole("button", { name: "Search" }).click();

  await expect.poll(async () => await rowIds(page), { timeout: 20_000 }).toEqual([newest]);
});

test("searching an id that does not exist says which id, and offers a way out", async ({
  page,
}) => {
  const { newest } = await idBounds();
  const absent = newest + 100_000;
  await openPortfolio(page);

  await page.locator("#loan-id-search").fill(String(absent));
  await page.getByRole("button", { name: "Search" }).click();

  const empty = page.getByTestId("servicing-empty");
  await expect(empty).toBeVisible({ timeout: 20_000 });
  // Naming the id is the point: "No loans match your filters" left an operator
  // unable to tell an absent loan from a filter hiding a present one.
  await expect(empty).toContainText(String(absent));
  await expect(empty.getByRole("button", { name: /Clear filters/i })).toBeVisible();
});

test("Clear returns the portfolio to its default view", async ({ page }) => {
  const { newest } = await idBounds();
  await openPortfolio(page);

  await page.locator("#loan-order").selectOption("oldest");
  await page.locator("#loan-id-search").fill("999999999");
  await page.getByRole("button", { name: "Search" }).click();
  await expect(page.getByTestId("servicing-empty")).toBeVisible({ timeout: 20_000 });

  await page.getByRole("button", { name: /^Clear$/ }).click();

  await expect.poll(async () => (await rowIds(page))[0], { timeout: 20_000 }).toBe(newest);
  await expect(page.locator("#loan-order")).toHaveValue("newest");
  await expect(page.locator("#loan-id-search")).toHaveValue("");
});

test("adjacent pages neither repeat nor skip a loan", async ({ page }) => {
  // The reason ordering is on `id` and not `opened_at`: a non-unique sort key
  // under LIMIT/OFFSET lets a row appear on one page and vanish from the next.
  const { total } = await idBounds();
  test.skip(total < 26, "needs more than one page of loans");
  await openPortfolio(page);

  const first = await rowIds(page);
  await page.getByRole("button", { name: /Next/i }).click();
  await expect.poll(async () => (await rowIds(page))[0], { timeout: 20_000 })
    .not.toBe(first[0]);
  const second = await rowIds(page);

  expect(new Set([...first, ...second]).size).toBe(first.length + second.length);
  // Strictly descending across the boundary: nothing was skipped between pages.
  expect(Math.min(...first)).toBeGreaterThan(Math.max(...second));
});

test("the status filter still works, in both directions", async ({ page }) => {
  await openPortfolio(page);

  for (const order of ["newest", "oldest"]) {
    await page.locator("#loan-order").selectOption(order);
    await page.locator("select").first().selectOption("current");
    await expect.poll(async () => (await rowIds(page)).length, { timeout: 20_000 })
      .toBeGreaterThan(0);

    const statuses = await page.locator("tbody tr td").allTextContents();
    expect(statuses.join(" ").toLowerCase()).toContain("current");
  }
});
