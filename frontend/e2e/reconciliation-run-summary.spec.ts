import { test, expect, Page } from "@playwright/test";
import { signInAsStaff } from "./fixtures";

/**
 * The latest run has to be readable at a glance, in front of a client.
 *
 * Every figure was already on the screen and correct. It was thirteen
 * sequential label/value rows, which is a list to scroll rather than a summary
 * to read: the four figures that answer "did this run find a problem" sat
 * between the ones describing how the run was performed.
 *
 * **These assertions compare the page against the API's own answer**, captured
 * from the response the page itself received, rather than recomputed from SQL.
 * A test that re-derives what the system already reports is a test that can be
 * green for the wrong reason, and this repository has produced three of those.
 *
 * What is NOT re-tested here: the candidate/break distinction
 * (`reconciliation-review-queue.spec.ts` owns it) and the break table's
 * contents (`reconciliation-run-evidence.spec.ts` owns those). This file owns
 * the presentation of the run summary, and asserts the break section is still
 * present rather than restating what it says.
 */

interface Latest {
  run: {
    outcome: string;
    started_at: string | null;
    finished_at: string | null;
    window_start: string | null;
    window_end: string | null;
    source: Record<string, unknown> | null;
    loans_compared: number;
    references_compared: number;
    unreferenced_captures: number;
    out_of_scope_captures: number;
    breaks_found: number;
    break_value: string;
    threshold_value: string;
    error_code: string | null;
    breaks_recorded: number;
  } | null;
  note: string;
}

/** Open the page and return the payload the page itself was served. */
async function openCapturing(page: Page): Promise<Latest> {
  await signInAsStaff(page, "admin");
  let captured: Latest | null = null;
  await page.route("**/reconciliation/latest", async (route) => {
    const response = await route.fetch();
    captured = (await response.json()) as Latest;
    await route.fulfill({ response });
  });
  await page.goto("/reconciliation");
  await expect(page.getByTestId("recon-latest-heading")).toBeVisible({
    timeout: 30_000,
  });
  await expect.poll(() => captured !== null, { timeout: 30_000 }).toBe(true);
  return captured!;
}

/** Serve a run of our own, to reach states the seed does not hold. */
async function serveRun(page: Page, run: Record<string, unknown>) {
  await page.route("**/reconciliation/latest", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ run, note: "" }),
    }),
  );
}

const A_RUN = {
  id: 1,
  outcome: "ok",
  started_at: "2026-08-30T10:00:00Z",
  finished_at: "2026-08-30T10:01:00Z",
  window_start: "2026-08-29",
  window_end: "2026-08-30",
  source: { file: "settlement.csv" },
  loans_compared: 0,
  references_compared: 0,
  unreferenced_captures: 0,
  out_of_scope_captures: 0,
  breaks_found: 0,
  break_value: "0.00",
  threshold_value: "100.00",
  error_code: null,
  breaks: [],
  breaks_recorded: 0,
  breaks_truncated: false,
  max_recorded_breaks: 50,
};

test("the four figures that judge the run are shown together", async ({ page }) => {
  const latest = await openCapturing(page);
  test.skip(latest.run === null, "no reconciliation run in this database");
  const run = latest.run!;

  await expect(page.getByTestId("recon-outcome")).toHaveText(run.outcome);
  await expect(page.getByTestId("recon-breaks-found")).toHaveText(
    String(run.breaks_found),
  );
  await expect(page.getByTestId("recon-break-value")).toHaveText(
    `$${run.break_value}`,
  );
  await expect(page.getByTestId("recon-threshold")).toHaveText(
    `$${run.threshold_value}`,
  );
});

test("the details say how the run was performed", async ({ page }) => {
  const latest = await openCapturing(page);
  test.skip(latest.run === null, "no reconciliation run in this database");
  const run = latest.run!;

  // Counts are compared exactly. The timestamps are formatted for a reader, so
  // they are asserted to be present rather than string-matched against ISO.
  await expect(page.getByTestId("recon-loans-compared")).toHaveText(
    String(run.loans_compared),
  );
  await expect(page.getByTestId("recon-references-compared")).toHaveText(
    String(run.references_compared),
  );
  await expect(page.getByTestId("recon-unreferenced")).toHaveText(
    String(run.unreferenced_captures),
  );
  await expect(page.getByTestId("recon-out-of-scope")).toHaveText(
    String(run.out_of_scope_captures),
  );
  await expect(page.getByTestId("recon-started")).not.toBeEmpty();
  await expect(page.getByTestId("recon-finished")).not.toBeEmpty();
  await expect(page.getByTestId("recon-source")).not.toBeEmpty();

  const window = page.getByTestId("recon-window");
  await expect(window).not.toBeEmpty();
  if (run.window_start && run.window_end) {
    await expect(window).toContainText(run.window_start);
    await expect(window).toContainText(run.window_end);
  }
  await expect(page.getByTestId("recon-run-details")).toBeVisible();
});

test("zero is rendered as zero, not as a blank", async ({ page }) => {
  // The failure this guards is specific: `{count && <span>{count}</span>}`
  // renders NOTHING for a zero. "Unreferenced captures: 0" says the run matched
  // every capture it saw; a blank says nothing and reads as reassurance.
  await signInAsStaff(page, "admin");
  await serveRun(page, A_RUN);
  await page.goto("/reconciliation");

  await expect(page.getByTestId("recon-breaks-found")).toHaveText("0", {
    timeout: 30_000,
  });
  await expect(page.getByTestId("recon-loans-compared")).toHaveText("0");
  await expect(page.getByTestId("recon-references-compared")).toHaveText("0");
  await expect(page.getByTestId("recon-unreferenced")).toHaveText("0");
  await expect(page.getByTestId("recon-out-of-scope")).toHaveText("0");
  await expect(page.getByTestId("recon-break-value")).toHaveText("$0.00");
});

test("an error code is shown when the run recorded one, and only then", async ({
  page,
}) => {
  await signInAsStaff(page, "admin");
  await serveRun(page, {
    ...A_RUN,
    outcome: "error",
    error_code: "SourceUnavailable",
  });
  await page.goto("/reconciliation");
  await expect(page.getByTestId("recon-error-code")).toHaveText(
    "SourceUnavailable",
    { timeout: 30_000 },
  );

  await page.unrouteAll({ behavior: "ignoreErrors" });
  await serveRun(page, A_RUN);
  await page.goto("/reconciliation");
  await expect(page.getByTestId("recon-latest-run")).toBeVisible({
    timeout: 30_000,
  });
  await expect(page.getByTestId("recon-error-code")).toHaveCount(0);
});

test("the transaction break section survives the summary rework", async ({ page }) => {
  const latest = await openCapturing(page);
  test.skip(latest.run === null, "no reconciliation run in this database");

  await expect(page.getByTestId("recon-breaks-heading")).toBeVisible();
  // One state or the other, decided by what the run recorded -- never both and
  // never neither.
  const table = page.getByTestId("recon-breaks-table");
  const none = page.getByTestId("recon-no-breaks");
  if (latest.run!.breaks_recorded > 0) {
    await expect(table).toBeVisible();
    await expect(table.locator("tbody tr")).toHaveCount(latest.run!.breaks_recorded);
    await expect(none).toHaveCount(0);
  } else {
    await expect(none).toBeVisible();
    await expect(table).toHaveCount(0);
  }
});

test("a phone-width viewport does not scroll sideways", async ({ page }) => {
  await page.setViewportSize({ width: 375, height: 800 });
  await signInAsStaff(page, "admin");
  // A source wide enough to push a rigid layout off-screen, so this measures
  // the layout rather than the seed's short filename.
  await serveRun(page, {
    ...A_RUN,
    source: {
      bucket: "meridian-settlement-exports-eu-west-1",
      file: "2026-08-30-daily-settlement-export.csv",
    },
  });
  await page.goto("/reconciliation");
  await expect(page.getByTestId("recon-latest-run")).toBeVisible({
    timeout: 30_000,
  });

  const overflow = await page.evaluate(
    () => document.documentElement.scrollWidth - document.documentElement.clientWidth,
  );
  expect(overflow, "horizontal overflow in CSS pixels").toBeLessThanOrEqual(1);
});

test("no control on this screen starts a reconciliation run", async ({ page }) => {
  // The scheduler owns when reconciliation happens. A control that ran because
  // somebody looked at the screen would not be a scheduled control, and a
  // "run now" button is how that arrives.
  await openCapturing(page);
  await expect(
    page.getByRole("button", { name: /run (reconciliation|now)/i }),
  ).toHaveCount(0);
});
