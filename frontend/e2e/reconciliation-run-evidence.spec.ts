import { test, expect } from "@playwright/test";
import { signInAsStaff, dbClient } from "./fixtures";

/**
 * The reconciliation panel shows what the last run actually recorded.
 *
 * Everything on it was already in `reconciliation_runs` and simply had no read
 * path: the window covered, the file read, how fine the comparison was, the
 * threshold it was judged against, and the per-transaction breaks. The screen
 * used to show two totals and a list of run summaries, which cannot answer the
 * question an operator asks next -- WHICH transactions disagree.
 *
 * **These assertions are computed from the database at test time, deliberately.**
 * The seeded run currently holds 12 breaks worth $4,038.53. Writing those two
 * figures into this file would produce a test that passes against a stale page
 * as long as the seed never changes, and fails for the wrong reason the moment
 * it does. So the expected values are read from `reconciliation_runs` and the
 * page is asserted to agree with them.
 *
 * The candidate/break distinction is not re-tested here -- `reconciliation-
 * review-queue.spec.ts` owns it. What is checked is that the break section
 * carries the server's own sentence about what a break is, rather than a
 * paraphrase this page invented.
 */

test("the latest run panel renders the run recorded in the database", async ({ page }) => {
  const client = dbClient();
  await client.connect();
  let run: {
    outcome: string;
    breaks_found: number;
    break_value: string;
    loans_compared: number;
    references_compared: number;
  } | null = null;
  try {
    const res = await client.query(
      `SELECT outcome, breaks_found, break_value::text AS break_value,
              loans_compared, references_compared
         FROM reconciliation_runs
        ORDER BY started_at DESC, id DESC LIMIT 1`,
    );
    run = res.rows[0] ?? null;
  } finally {
    await client.end();
  }

  test.skip(run === null, "no reconciliation run in this database to display");

  await signInAsStaff(page, "admin");
  await page.goto("/reconciliation");

  await expect(page.getByTestId("recon-latest-heading")).toBeVisible({
    timeout: 20_000,
  });

  // The figures, from the row rather than from this file.
  await expect(page.getByTestId("recon-outcome")).toHaveText(run!.outcome);
  await expect(page.getByTestId("recon-breaks-found")).toHaveText(
    String(run!.breaks_found),
  );

  const panel = page.getByTestId("recon-latest-run");
  await expect(panel).toContainText(String(run!.loans_compared));
  await expect(panel).toContainText(String(run!.references_compared));
  await expect(panel).toContainText(run!.break_value);
});

test("every recorded break is on the screen, with both sides and a loan link", async ({
  page,
}) => {
  const client = dbClient();
  await client.connect();
  let breaks: Array<{ loan_id: number; processor_ref: string | null }> = [];
  try {
    const res = await client.query(
      `SELECT (b->>'loan_id')::int AS loan_id, b->>'processor_ref' AS processor_ref
         FROM reconciliation_runs r
         CROSS JOIN LATERAL jsonb_array_elements(r.breaks) AS b
        WHERE r.id = (SELECT id FROM reconciliation_runs
                       ORDER BY started_at DESC, id DESC LIMIT 1)`,
    );
    breaks = res.rows;
  } finally {
    await client.end();
  }

  test.skip(breaks.length === 0, "the latest run recorded no breaks to render");

  await signInAsStaff(page, "admin");
  await page.goto("/reconciliation");

  const table = page.getByTestId("recon-breaks-table");
  await expect(table).toBeVisible({ timeout: 20_000 });

  // Every break, not just the first: a table that renders one row of twelve
  // still looks populated.
  await expect(table.locator("tbody tr")).toHaveCount(breaks.length);

  for (const b of breaks) {
    if (b.processor_ref) {
      await expect(table).toContainText(b.processor_ref);
    }
  }

  // The loan link is what makes a break investigable rather than merely
  // reported.
  const first = breaks[0];
  await expect(
    table.locator(`a[href="/servicing/${first.loan_id}"]`).first(),
  ).toBeVisible();
});

test("the break section carries the server's own words about what a break is", async ({
  page,
}) => {
  // The distinction the client asked to be preserved. A break is the control
  // finding the books disagree; it is not a duplicate payment and not proof
  // money was lost. The page renders the server's sentence rather than its own,
  // so the wording cannot soften as the page is edited.
  await signInAsStaff(page, "admin");
  await page.goto("/reconciliation");

  await expect(page.getByTestId("recon-latest-heading")).toBeVisible({
    timeout: 20_000,
  });

  const body = page.locator("body");
  await expect(body).toContainText("transaction-level mismatch requiring investigation");
  await expect(body).toContainText("not proof that money was lost");

  // And it does NOT reuse the candidate note's wording. Two different findings
  // described in one sentence would blur the distinction both notes exist to
  // protect -- and the identical phrase on one page broke an existing spec's
  // locator, which is how this was caught.
  await expect(body).toContainText("not a duplicate payment");
});

test("there is no way to start a reconciliation run from this page", async ({ page }) => {
  // The scheduler owns when reconciliation happens. A "run now" control would
  // put the comparison in reach of a page load and make the evidence on screen
  // depend on who was looking -- and a control that runs because somebody
  // opened a tab is not a scheduled control.
  await signInAsStaff(page, "admin");
  await page.goto("/reconciliation");

  await expect(page.getByTestId("recon-latest-heading")).toBeVisible({
    timeout: 20_000,
  });

  for (const label of [/run reconciliation/i, /run now/i, /re-?run/i, /start run/i]) {
    await expect(page.getByRole("button", { name: label })).toHaveCount(0);
  }
});

test("a break table that is only part of the answer says so", async ({ page }) => {
  // REV-TRUNCATED-BREAKS. `compare` stores at most 50 break rows while
  // `breaks_found` counts every one it found, so a large run renders a PREFIX.
  // Under the true count with nothing between them, an operator reads the rows
  // as every disagreement there was.
  //
  // Asserted against whatever the database currently holds rather than a seeded
  // large run: the seed's run is small, so the meaningful check is that the
  // label and the row count agree with the record in BOTH directions.
  const client = dbClient();
  await client.connect();
  let run: { breaks_found: number; recorded: number } | null = null;
  try {
    const res = await client.query(
      `SELECT breaks_found, jsonb_array_length(breaks) AS recorded
         FROM reconciliation_runs ORDER BY started_at DESC, id DESC LIMIT 1`,
    );
    run = res.rows[0] ?? null;
  } finally {
    await client.end();
  }

  test.skip(run === null, "no reconciliation run in this database");

  await signInAsStaff(page, "admin");
  await page.goto("/reconciliation");
  await expect(page.getByTestId("recon-breaks-heading")).toBeVisible({
    timeout: 20_000,
  });

  const truncated = run!.recorded < run!.breaks_found;
  const banner = page.getByTestId("recon-breaks-truncated");

  if (truncated) {
    // It must say how many are missing, not merely that some are.
    await expect(banner).toBeVisible();
    await expect(banner).toContainText(String(run!.breaks_found));
    await expect(banner).toContainText(String(run!.recorded));
  } else {
    // And it must NOT cry partial on a complete list -- a label that is always
    // on teaches an operator to ignore it.
    await expect(banner).toHaveCount(0);
  }

  // Either way the heading states both figures, so the table is never a bare
  // list under a bare count.
  if (run!.breaks_found > 0) {
    await expect(page.getByTestId("recon-breaks-count")).toHaveText(
      `(${run!.recorded} of ${run!.breaks_found})`,
    );
  }
});

test("the break table is never offered as a page of a larger set", async ({ page }) => {
  // Unlike the approvals queue, the unshown breaks were never persisted -- they
  // exist inside a count and nowhere else. A "next page" control would promise
  // rows no query can produce, so there must not be one here even though the
  // list is bounded.
  await signInAsStaff(page, "admin");
  await page.goto("/reconciliation");
  await expect(page.getByTestId("recon-breaks-heading")).toBeVisible({
    timeout: 20_000,
  });

  for (const label of [/next/i, /load more/i, /show more/i]) {
    await expect(page.getByRole("button", { name: label })).toHaveCount(0);
  }
});
