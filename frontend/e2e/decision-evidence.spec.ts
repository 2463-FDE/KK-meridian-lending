import { test, expect } from "@playwright/test";
import {
  signInAsStaff, signInAsBorrower, dbClient, fictionalApplicant,
  submitApplication, currentAppId, getDecision, resolveReferAsStaff,
  REFER_BAND_INCOME,
} from "./fixtures";

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

test("the two scores are labelled so neither can be read as the other", async ({
  page,
}) => {
  // Both scores stay -- they are different evidence and a reviewer needs both.
  // What changed is the labelling: "Model score" beside "Bureau score" invited
  // reading them as two credit scores, one of them adjusted. The underwriting
  // model score is this system's own output and is not a credit score of any
  // kind, so the panel says which is which and says the difference out loud.
  await signInAsStaff(page, "underwriter");
  await page.route("**/applications/*/history", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        decision: {
          outcome: "approve", model_decision: "approve",
          model_score: 712, model_version: "v2.1.0-stub",
          bureau_score: 690, reason_codes: [], occurred_at: null,
        },
      }),
    }),
  );
  await page.goto("/underwriting/4471");

  const panel = page.getByTestId("decision-evidence");
  await expect(panel).toContainText("Underwriting model score", { timeout: 60_000 });
  await expect(panel).toContainText("Credit bureau score");
  await expect(page.getByTestId("evidence-model-score")).toHaveText("712");
  await expect(page.getByTestId("evidence-bureau-score")).toHaveText("690");

  await expect(page.getByTestId("evidence-model-score-note")).toContainText(
    "not a bureau credit score",
  );

  // The claims that must never attach to the model score. Asserted as text
  // rather than trusted to review, because this is exactly the wording that
  // drifts back in when someone edits a label for brevity.
  const text = (await panel.innerText()).toLowerCase();
  for (const claim of ["fico", "adjusted credit", "enhanced credit"]) {
    expect(text, `"${claim}" must not describe the underwriting model score`)
      .not.toContain(claim);
  }
});

test("the demo stub's derivation is stated, and only for the stub", async ({
  page,
}) => {
  // The deterministic demo stub really is computed from the bureau score and
  // stated income (`_stub_model_score` in decision-service), so saying so is
  // accurate AND useful -- it is the answer to "why is that number near the
  // bureau score". A licensed scorer's output is the provider model's own, and
  // this copy must not claim that formula for it. The stub is identifiable
  // because its `model_version` carries the `-stub` suffix, which is the
  // contract RF-1 established.
  await signInAsStaff(page, "underwriter");

  const withVersion = (version: string) =>
    page.route("**/applications/*/history", (route) =>
      route.fulfill({
        status: 200,
        contentType: "application/json",
        body: JSON.stringify({
          decision: {
            outcome: "approve", model_decision: "approve",
            model_score: 712, model_version: version,
            bureau_score: 690, reason_codes: [], occurred_at: null,
          },
        }),
      }),
    );

  await withVersion("v2.1.0-stub");
  await page.goto("/underwriting/4471");
  await expect(page.getByTestId("evidence-model-score-note")).toContainText(
    "derived from the credit bureau score and stated income",
    { timeout: 60_000 },
  );

  await page.unrouteAll({ behavior: "ignoreErrors" });
  await withVersion("v2.1.0");
  await page.goto("/underwriting/4471");
  const note = page.getByTestId("evidence-model-score-note");
  await expect(note).toContainText("not a bureau credit score");
  await expect(note).not.toContainText("derived from");
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
          model_decision: "deny",
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
  // And nothing explains a score that is not there. The note says what the
  // underwriting score IS; with no score recorded it had no referent, which is
  // the majority case in this database rather than an edge one.
  await expect(page.getByTestId("evidence-model-score-note")).toHaveCount(0);
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
  //
  // Both callers, because they fail for different reasons and only one of them
  // was checked before. R1-MINOR: the first version sent no credentials at all,
  // so it proved the route rejects an ANONYMOUS caller -- which says nothing
  // about a signed-in borrower, the case the staff gate actually exists for.
  const anonymous = await page.request.get(
    "http://localhost:8000/los/applications/4471/history",
  );
  expect(anonymous.status()).toBeGreaterThanOrEqual(400);
  expect(anonymous.status()).toBeLessThan(500);

  await signInAsBorrower(page);
  // `meridian.token`, mirroring TOKEN_KEY in `frontend/lib/api.ts`. The
  // assertion below is what keeps that mirror honest: if the key is ever
  // renamed this test fails loudly on a missing token rather than quietly
  // sending `Bearer null` and passing on a 401 it did not earn.
  const token = await page.evaluate(() => localStorage.getItem("meridian.token"));
  expect(token, "borrower session token").toBeTruthy();

  const asBorrower = await page.request.get(
    "http://localhost:8000/los/applications/4471/history",
    { headers: { Authorization: `Bearer ${token}` } },
  );
  expect(asBorrower.status()).toBeGreaterThanOrEqual(400);
  expect(asBorrower.status()).toBeLessThan(500);
});

test("the model's decision and the current outcome are shown separately when they differ", async ({
  page,
}) => {
  // R1-BLOCKER. `decisions.outcome` is staff-mutable -- the manual review route
  // runs `UPDATE decisions SET outcome = ...` and writes NO new
  // `decision_events` row -- while model version, model score and reason codes
  // come from that untouched event. Rendering one "Recorded outcome" from
  // `decisions` beside those fields therefore attributed a STAFF decision to a
  // named model version. Two applications in the seeded database are already in
  // that state, so this was live, not hypothetical.
  //
  // Built rather than found: this drives a real refer to a real staff approval,
  // so the divergence is created by the product's own path and the assertion
  // does not depend on which rows a seed happens to carry.
  const applicant = fictionalApplicant("Devi", false, REFER_BAND_INCOME);
  await submitApplication(page, applicant);
  const appId = await currentAppId(page);
  await getDecision(page);

  await signInAsStaff(page);
  await resolveReferAsStaff(
    page, appId, "approve", "Reviewed updated documentation; band reconsidered",
  );

  // NO RELOAD between the approval and these assertions, deliberately.
  //
  // `resolveReferAsStaff` leaves the browser on this page, so this also holds
  // R1-MAJOR: the panel used to load once on mount, and the handlers that
  // CHANGE a decision refreshed the application and the lifecycle strip but not
  // the evidence. A reader saw "Decision finalized" above a panel still
  // reporting the pre-review state. Navigating first would have hidden that,
  // and would have tested a page load rather than the handler.
  await expect(page.getByTestId("evidence-model-decision")).toHaveText("refer", {
    timeout: 20_000,
  });
  await expect(page.getByTestId("evidence-outcome")).toHaveText("approve");
  await expect(page.getByTestId("evidence-outcome-differs")).toBeVisible();

  // And it survives the reload, so the refresh above reflected the database
  // rather than local state the handler happened to hold.
  await openApplication(page, Number(appId));
  await expect(page.getByTestId("evidence-model-decision")).toHaveText("refer");
  await expect(page.getByTestId("evidence-outcome")).toHaveText("approve");
});

test("an application with no model event does not attribute its outcome to a model", async ({
  page,
}) => {
  // The COMMON case in this database, not an edge one: 306 of 328 decisions
  // carry no `decision_events` row at all. Before this round those rendered the
  // outcome under a "Recorded outcome" heading in a panel of model evidence,
  // with every model field blank -- which reads as a model decision whose
  // details went missing, rather than as no model event.
  await signInAsStaff(page, "underwriter");
  await page.route("**/applications/*/history", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        decision: {
          outcome: "approve",
          model_decision: null,
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

  await expect(page.getByTestId("evidence-model-decision")).toHaveText("not recorded");
  await expect(page.getByTestId("evidence-outcome")).toHaveText("approve");
  await expect(page.getByTestId("evidence-no-model-record")).toBeVisible();
  await expect(page.getByTestId("evidence-outcome-differs")).toHaveCount(0);
});
