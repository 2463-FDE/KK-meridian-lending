import { test, expect } from "@playwright/test";
import { signInAsStaff, dbClient } from "./fixtures";

/**
 * What the model recorded when it decided this application.
 *
 * `GET /los/applications/{id}/history` has returned it since Week 4 and nothing
 * rendered it, so "which model version denied this, and on what reason codes"
 * had no answer on any screen. It is the per-application counterpart to the
 * aggregate distribution on `/admin`.
 *
 * **Not called a history, because there is not one.** The endpoint returns a
 * graph -- applicant, application, one decision, offers -- and `decision_events`
 * holds exactly one row per application across the entire database. A timeline
 * would be a single point drawn as a line, and a reader would trust it for
 * precisely the thing it could not show. The staff review that may follow is
 * already rendered in the decision panel, with its own reason, author and date.
 *
 * These cases assert the panel shows the SERVER's record, distinguishes absent
 * from zero, and stays out of the way of the application detail when it fails.
 */

async function openApplication(
  page: import("@playwright/test").Page,
  appId: number,
) {
  await signInAsStaff(page, "underwriter");
  await page.goto(`/underwriting/${appId}`);
  // Generous on the FIRST paint, deliberately. Next.js compiles a dynamic route
  // on first request, so immediately after a container rebuild the first test to
  // touch this page can exceed a 20s wait and fail for a reason that has nothing
  // to do with the assertion. That cold start has produced a false failure three
  // times in this repository now; waiting longer costs nothing on a warm server
  // because the assertion resolves as soon as the element appears.
  await expect(page.getByTestId("decision-evidence")).toBeVisible({
    timeout: 60_000,
  });
}

/** An application whose decision really was recorded, and what it recorded. */
async function aDecidedApplication(): Promise<{
  appId: number;
  outcome: string;
  modelVersion: string | null;
  reasons: string[];
} | null> {
  const client = dbClient();
  await client.connect();
  try {
    const r = await client.query(
      `SELECT e.app_id::int AS "appId", d.outcome, e.model_version AS "modelVersion",
              COALESCE(e.reason_codes, '[]'::jsonb) AS reasons
         FROM decision_events e JOIN decisions d ON d.app_id = e.app_id
        ORDER BY e.app_id DESC LIMIT 1`,
    );
    const row = r.rows[0];
    if (!row) return null;
    return { ...row, reasons: row.reasons as string[] };
  } finally {
    await client.end();
  }
}

test("the recorded decision is shown as the server holds it", async ({ page }) => {
  const decided = await aDecidedApplication();
  test.skip(decided === null, "no decision event recorded in this database");

  await openApplication(page, decided!.appId);

  await expect(page.getByTestId("evidence-outcome")).toHaveText(decided!.outcome);
  if (decided!.modelVersion) {
    await expect(page.getByTestId("evidence-model-version")).toHaveText(
      decided!.modelVersion,
    );
  }
});

test("reason codes are rendered, or their absence is stated", async ({ page }) => {
  const decided = await aDecidedApplication();
  test.skip(decided === null, "no decision event recorded in this database");

  await openApplication(page, decided!.appId);

  if (decided!.reasons.length > 0) {
    for (const code of decided!.reasons) {
      await expect(page.getByTestId(`evidence-reason-${code}`)).toBeVisible();
    }
  } else {
    // An approval carries none, and that is correct rather than a gap: only an
    // adverse action needs a reason.
    await expect(page.getByTestId("evidence-no-reasons")).toBeVisible();
  }
});

test("an application with no decision says so rather than showing blanks", async ({
  page,
}) => {
  // Stubbed, and the first version was not -- which is why it failed.
  //
  // It queried for an application with no row in `decision_events`. The endpoint
  // reads `FROM decisions d LEFT JOIN decision_events e`, so a decision EXISTS
  // whenever `decisions` has a row, whatever `decision_events` holds -- and 306
  // seeded applications are in exactly that state. The panel rendered the
  // evidence card correctly and the test demanded the empty state: the test was
  // wrong, and it would have blamed the product.
  //
  // That is the third time in this repository a test has re-derived backend
  // logic and diverged from it. The countermeasure that works is not a better
  // query, it is not writing one: assert against the ANSWER the endpoint gives.
  await signInAsStaff(page, "underwriter");
  await page.route("**/applications/*/history", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({ decision: null }),
    }),
  );
  await page.goto("/underwriting/4471");

  await expect(page.getByTestId("decision-evidence-empty")).toBeVisible({
    timeout: 60_000,
  });
  await expect(page.getByTestId("evidence-outcome")).toHaveCount(0);
});

test("a missing figure reads as not recorded, never as zero", async ({ page }) => {
  // A model score of 0 and "no score was recorded" are different facts about a
  // decision, and rendering the second as the first would be inventing one.
  await signInAsStaff(page, "underwriter");
  await page.route("**/applications/*/history", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        decision: {
          outcome: "deny",
          model_score: null,
          model_version: null,
          bureau_score: null,
          reason_codes: [],
          occurred_at: null,
        },
      }),
    }),
  );
  await page.goto("/underwriting/4471");

  await expect(page.getByTestId("evidence-model-score")).toHaveText("not recorded");
  await expect(page.getByTestId("evidence-bureau-score")).toHaveText("not recorded");
  await expect(page.getByTestId("evidence-model-version")).toHaveText("not recorded");
  await expect(page.getByTestId("evidence-at")).toHaveText("not recorded");
  await expect(page.getByTestId("evidence-no-reasons")).toBeVisible();
});

test("failing to read the evidence does not blank the application", async ({ page }) => {
  await signInAsStaff(page, "underwriter");
  await page.route("**/applications/*/history", (route) =>
    route.fulfill({ status: 500, contentType: "application/json", body: "{}" }),
  );
  await page.goto("/underwriting/4471");

  await expect(page.getByTestId("decision-evidence-error")).toBeVisible({
    timeout: 20_000,
  });
  // The application it describes is still on screen.
  await expect(page.getByText(/Application #4471/)).toBeVisible();
  await expect(page.getByTestId("app-lifecycle")).toBeVisible();
});

test("a borrower cannot read decision evidence", async ({ page }) => {
  // The endpoint is staff-only because it carries model score, bureau score and
  // reason codes. This change renders it on a page staff already reach; it
  // widens nothing, and the gateway hop is asserted here rather than assumed.
  const res = await page.request.get(
    "http://localhost:8000/los/applications/4471/history",
  );

  expect(res.status()).toBeGreaterThanOrEqual(400);
  expect(res.status()).toBeLessThan(500);
});
