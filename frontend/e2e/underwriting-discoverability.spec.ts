import { test, expect } from "@playwright/test";
import { dbClient, signInAsStaff } from "./fixtures";

/**
 * An application you can decide must be an application you can find.
 *
 * The underwriting console's search box filtered the rows ALREADY FETCHED --
 * `page.tsx` said so in its own comment -- so an application outside the current
 * 25 could not be found by typing its id. Applications are ordered by `id`, ids
 * ascend at submission, and the page holds 25, so anything but the newest
 * handful sat on a later page and the search reported nothing.
 *
 * Nothing about the application was wrong. The list was. This is the same defect
 * the servicing portfolio carried until #120, on the screen an underwriter
 * starts their day on.
 *
 * These cases pin the fix from the underwriter's side: newest first by default,
 * and an id looked up across the whole pipeline rather than the page in hand.
 * The SQL-level facts -- direction, both filters reaching the COUNT, the enum
 * being validated -- are pinned in
 * `services/origination-service/tests/test_application_list_ordering_and_lookup.py`.
 *
 * This spec only READS the pipeline. It submits nothing and decides nothing, so
 * it consumes no fixture application and can be re-run against one database.
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

async function idBounds(): Promise<{ newest: number; oldest: number; total: number }> {
  return withDb(async (c) => {
    const r = await c.query(
      "SELECT max(id)::int AS newest, min(id)::int AS oldest, count(*)::int AS total FROM applications",
    );
    return r.rows[0];
  });
}

const rowIds = async (page: import("@playwright/test").Page) =>
  (await page.locator('tbody tr a[href^="/underwriting/"]').allTextContents())
    .map((t) => Number(t.replace(/\D/g, "")))
    .filter((n) => Number.isFinite(n) && n > 0);

async function openConsole(page: import("@playwright/test").Page) {
  await signInAsStaff(page, "underwriter");
  await page.goto("/underwriting");
  await expect(page.getByRole("heading", { name: /underwriting console/i })).toBeVisible({
    timeout: 20_000,
  });
  // Wait for a DATA row, not merely a row: the table renders a full-width
  // "Loading…" cell first, which is a `tbody tr` and has no id in it. Waiting on
  // the generic selector let the first assertion run against the placeholder.
  await expect(
    page.locator('tbody tr a[href^="/underwriting/"]').first(),
  ).toBeVisible({ timeout: 20_000 });
}

test("the newest application is on the first page by default", async ({ page }) => {
  const { newest, total } = await idBounds();
  test.skip(total === 0, "no applications in this database");

  await openConsole(page);

  const ids = await rowIds(page);
  expect(ids.length).toBeGreaterThan(0);
  // Not merely "present somewhere" -- first, because newest-first is the point.
  expect(ids[0]).toBe(newest);
});

test("the oldest application is found from page one by typing its id", async ({ page }) => {
  // The defect, from the operator's side. The oldest id is the one furthest
  // from page one, so under the old client-side filter this search returned
  // nothing at all.
  const { oldest, newest, total } = await idBounds();
  test.skip(total < 2, "need at least two applications to have an off-page one");

  await openConsole(page);

  const before = await rowIds(page);
  expect(before).not.toContain(oldest);
  expect(before).toContain(newest);

  await page.getByLabel("Application ID").fill(String(oldest));
  await page.getByRole("button", { name: "Search", exact: true }).click();

  await expect(page.locator('tbody tr a[href^="/underwriting/"]')).toHaveCount(1);
  expect(await rowIds(page)).toEqual([oldest]);
});

test("searching an id that does not exist says which id, and offers a way out", async ({
  page,
}) => {
  const { newest } = await idBounds();
  const missing = newest + 99_999;

  await openConsole(page);
  await page.getByLabel("Application ID").fill(String(missing));
  await page.getByRole("button", { name: "Search", exact: true }).click();

  // Naming the id is the difference between "it is not here" and "your filters
  // are hiding it".
  await expect(page.locator("tbody")).toContainText(String(missing));
  await expect(page.getByRole("button", { name: /clear filters/i })).toBeVisible();
});

test("Clear returns the console to its default view", async ({ page }) => {
  const { newest, oldest, total } = await idBounds();
  test.skip(total < 2, "need at least two applications");

  await openConsole(page);
  await page.getByLabel("Application ID").fill(String(oldest));
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(page.locator('tbody tr a[href^="/underwriting/"]')).toHaveCount(1);

  await page.getByRole("button", { name: "Clear", exact: true }).click();

  // Clear triggers a refetch, so poll rather than reading the table that is
  // still on screen from the filtered query.
  await expect(async () => {
    const ids = await rowIds(page);
    expect(ids.length).toBeGreaterThan(1);
    expect(ids[0]).toBe(newest);
  }).toPass({ timeout: 15_000 });
});

test("sorting oldest first puts the oldest application at the top", async ({ page }) => {
  const { oldest, total } = await idBounds();
  test.skip(total < 2, "need at least two applications");

  await openConsole(page);
  await page.getByLabel("Sort").selectOption("oldest");

  await expect(async () => {
    const ids = await rowIds(page);
    expect(ids[0]).toBe(oldest);
  }).toPass({ timeout: 15_000 });
});

test("adjacent pages neither repeat nor skip an application", async ({ page }) => {
  // A non-unique sort key under LIMIT/OFFSET lets a row appear on one page and
  // vanish from the next. Ordering is on `id` for that reason, and this asserts
  // the consequence rather than the column.
  const { total } = await idBounds();
  test.skip(total <= 25, "only one page of applications in this database");

  await openConsole(page);
  const first = await rowIds(page);

  await page.getByRole("button", { name: /next/i }).click();
  await expect(async () => {
    const second = await rowIds(page);
    expect(second.length).toBeGreaterThan(0);
    expect(second).not.toEqual(first);
  }).toPass({ timeout: 15_000 });

  const second = await rowIds(page);

  const overlap = first.filter((id) => second.includes(id));
  expect(overlap, "an application appeared on two adjacent pages").toEqual([]);

  // Repeats are only half of it, and descent is not the other half.
  //
  // A row DROPPED at the boundary leaves no overlap, and it also leaves the two
  // pages strictly descending -- so `min(first) > max(second)` passes while an
  // application has gone missing. Verified rather than reasoned: shortening the
  // page query to `limit - 1` skips exactly one row between pages, and that
  // mutation PASSES the descent check.
  //
  // What actually catches it is contiguity. The two pages together must be the
  // first N ids of the ordered pipeline, so a gap fails on the values rather
  // than on their direction.
  const expected = await withDb(async (c) => {
    const r = await c.query(
      "SELECT id::int AS id FROM applications ORDER BY id DESC LIMIT $1",
      [first.length + second.length],
    );
    return r.rows.map((row: { id: number }) => row.id);
  });
  expect(
    [...first, ...second],
    "an application fell between two adjacent pages",
  ).toEqual(expected);
});

test("the status filter still works alongside the id lookup", async ({ page }) => {
  // Both filters compose server-side. A status that silently dropped the id --
  // or the reverse -- would show rows the underwriter did not ask for.
  const row = await withDb(async (c) => {
    const r = await c.query(
      "SELECT id::int AS id, status FROM applications WHERE status IS NOT NULL ORDER BY id ASC LIMIT 1",
    );
    return r.rows[0] ?? null;
  });
  test.skip(row === null, "no application with a status to filter on");

  await openConsole(page);
  await page.getByLabel("Application ID").fill(String(row.id));
  await page.getByRole("button", { name: "Search", exact: true }).click();
  await expect(page.locator('tbody tr a[href^="/underwriting/"]')).toHaveCount(1);

  // A status that cannot match the found row must empty the result rather than
  // ignore one of the two filters.
  const options = await page.getByLabel("Status").locator("option").all();
  const values = await Promise.all(options.map((o) => o.getAttribute("value")));
  const other = values.find((v) => v && v !== row.status);
  test.skip(!other, "no second status option to contrast with");

  await page.getByLabel("Status").selectOption(other!);

  // The row must DISAPPEAR: it has one status, and a different one was asked
  // for alongside its id, so the two filters together match nothing.
  //
  // The first version of this asserted the tbody still contained the id, which
  // passed whether the filters composed or not -- the empty-state copy names
  // the searched id too ("No application #4471 matches..."). An assertion that
  // holds in both the fixed and the broken case proves nothing, which is the
  // defect this file exists to catch in the product.
  await expect(page.locator('tbody tr a[href^="/underwriting/"]')).toHaveCount(0);
  await expect(page.getByRole("button", { name: /clear filters/i })).toBeVisible();
});
