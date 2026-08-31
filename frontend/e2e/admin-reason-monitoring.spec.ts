import { test, expect } from "@playwright/test";
import { signInAsStaff } from "./fixtures";

/**
 * Adverse-action reason monitoring, on the screen an admin actually opens.
 *
 * `reason_distribution.py` has answered spec 0003 §1.3 since Week 8 -- which
 * adverse-action reasons the model really emitted, per model version, over a
 * stated window -- and nothing rendered it. The regulator-facing question
 * "show me your reason distribution" had an API answer and no demo answer.
 *
 * **The maths is not re-tested here and is not re-implemented in the browser.**
 * `services/origination-service/tests/test_reason_distribution.py` owns the
 * computation. What this file checks is that the panel shows the SERVER's
 * numbers, keeps model versions apart, and does not overclaim.
 *
 * The overclaiming half is the point. The client prohibited runtime
 * protected-class data and inferred proxies -- ZIP/ZIP3 by name -- and the
 * runtime ZIP screen was retired on that instruction. A panel that drifted into
 * calling this a fairness result would undo that decision in copy while the code
 * stayed correct, so the qualifier and the absence of fairness language are
 * asserted, not assumed.
 */

async function openAdmin(page: import("@playwright/test").Page) {
  await signInAsStaff(page, "admin");
  await page.goto("/admin");
  await expect(page.getByTestId("reason-monitoring-heading")).toBeVisible({
    timeout: 20_000,
  });
}

interface ReasonVersion {
  model_version: string;
  distinct_reasons: number;
  missing_reason: number;
  reason_frequency: Record<string, number>;
}

/**
 * Open `/admin` and capture what the endpoint ACTUALLY answered.
 *
 * Review finding R1-MINOR-1: the first version read `decision_events` directly
 * and re-derived the figures in SQL. That quietly disagreed with the service --
 * `reason_distribution.py` drops blank strings before choosing the principal
 * reason, so `["   "]` is a missing reason to it and a distinct reason to a
 * naive `reason_codes->>0`. A correct panel could have failed the test.
 *
 * The deeper problem was the shape: the test reasoned about the DATA when its
 * subject is whether the panel faithfully renders the ANSWER. That is the same
 * coupling that made the earlier `test.skip` fragile. So the response is
 * intercepted and passed straight through, and the UI is compared against it.
 * Row-level maths stays where it belongs, in
 * `services/origination-service/tests/test_reason_distribution.py`.
 */
async function openAdminCapturing(
  page: import("@playwright/test").Page,
): Promise<{ versions: ReasonVersion[]; window: { since: string | null; until: string | null } }> {
  await signInAsStaff(page, "admin");
  let payload: { versions: ReasonVersion[]; window: { since: string | null; until: string | null } } | null =
    null;
  await page.route("**/fair-lending/reason-distribution", async (route) => {
    const response = await route.fetch();
    payload = await response.json();
    await route.fulfill({ response });
  });
  await page.goto("/admin");
  await expect(page.getByTestId("reason-monitoring-heading")).toBeVisible({
    timeout: 20_000,
  });
  await expect
    .poll(() => payload !== null, { timeout: 20_000 })
    .toBe(true);
  return payload!;
}

test("the panel is named for what it measures, and carries its qualifier", async ({
  page,
}) => {
  await openAdmin(page);

  // Named "adverse-action reason monitoring", not a fairness dashboard.
  await expect(page.getByTestId("reason-monitoring-heading")).toHaveText(
    /adverse-action reason monitoring/i,
  );

  const qualifier = page.getByTestId("reason-monitoring-qualifier");
  await expect(qualifier).toContainText(
    "not a protected-class disparity analysis",
  );
  await expect(qualifier).toContainText("production fairness determination");

  // WHAT IS COUNTED. The distribution reads `decision_events` and filters on
  // that table's own `decision`, so a denial with no model event -- one issued
  // on manual review, for instance -- is not in these figures. That is right
  // for model governance and wrong only if a reader takes the heading to mean
  // every adverse action a person received. The page says which it is.
  //
  // Asserted as ONE contiguous claim, not three scattered phrases. A first
  // version checked "recorded by the model", "manual review" and "not counted
  // here" separately, and a mutation that reversed the meaning of the sentence
  // -- while leaving those fragments in place -- passed. Substring checks over
  // a sentence do not test what the sentence says.
  const scope = page.getByTestId("reason-monitoring-scope");
  await expect(scope).toContainText(
    "These counts cover model decision events recorded as a denial, per model version.",
  );
  // The boundary is DENY events versus REFER events, not the presence of an
  // event. R1-MAJOR: the first version of this sentence said a manual-review
  // denial "carries no model reason codes", which is false -- such a denial
  // resolves a prior model `refer`, and every refer event in this database
  // carries reason codes. A disclosure that is wrong about the case it names is
  // worse than no disclosure, so the exact claim is asserted here.
  await expect(scope).toContainText(
    "a denial recorded on manual review after the model referred",
  );
  await expect(scope).toContainText("outside this deny-only distribution");
  await expect(scope).toContainText(
    "even where the model event carries reason codes of its own",
  );
  // Neither the reversed claim nor the retracted one may reappear.
  await expect(scope).not.toContainText("is counted here");
  await expect(scope).not.toContainText("carries no model reason codes");
});

test("no fairness verdict, protected class or proxy is rendered", async ({ page }) => {
  // The claims that would undo the client's own instruction. Checked against
  // the whole page rather than the panel, because a governance claim is just
  // as wrong in a KPI tile as in the section it belongs to.
  await openAdmin(page);

  const body = page.locator("body");
  for (const forbidden of [
    /\bZIP\b/i,
    /ZIP3/i,
    /protected class(?!\s+disparity analysis)/i,
    /\brace\b/i,
    /\bethnicit/i,
    /\bgender\b/i,
    /model is fair\b/i,
    /fair[- ]lending compliant/i,
    /no discrimination/i,
    /disparate impact/i,
  ]) {
    await expect(body).not.toHaveText(forbidden);
  }
});

test("the panel agrees with the decision record, whatever it says", async ({
  page,
}) => {
  // **This case must never skip, and an earlier version of it did.**
  //
  // It was written as `test.skip(versions.length === 0)`, which looked safe and
  // was not: `db/init/002_seed.sql` creates NO `decision_events` at all. Every
  // denial in this database is produced by another spec -- `denied-workflow`
  // among them -- and this file sorts FIRST alphabetically, so on a genuinely
  // fresh seed it ran before any denial existed and skipped both of its own
  // load-bearing assertions. Green, and proving nothing, in exactly the
  // environment CI uses.
  //
  // So both branches are asserted instead. No decisions is a real state with a
  // real rendering, and checking it is worth more than skipping past it.
  const answer = await openAdminCapturing(page);

  if (answer.versions.length === 0) {
    await expect(page.getByTestId("reason-monitoring-empty")).toBeVisible();
    return;
  }

  for (const v of answer.versions) {
    const card = page.getByTestId(`reason-version-${v.model_version}`);
    await expect(card, `model version ${v.model_version} is not shown`).toBeVisible();

    // The no-reason count: spec 0003 says it should be zero, and it is the one
    // figure that is a defect rather than a statistic.
    await expect(
      page.getByTestId(`reason-missing-${v.model_version}`),
    ).toHaveText(String(v.missing_reason));

    await expect(
      page.getByTestId(`reason-distinct-${v.model_version}`),
    ).toHaveText(String(v.distinct_reasons));

    // One row per reason the server reported, and the codes themselves --
    // rendered, not re-derived.
    const reasons = Object.keys(v.reason_frequency);
    if (reasons.length > 0) {
      const table = page.getByTestId(`reason-table-${v.model_version}`);
      await expect(table.locator("tbody tr")).toHaveCount(reasons.length);
      for (const [reason, count] of Object.entries(v.reason_frequency)) {
        await expect(
          table.locator("tbody tr").filter({ hasText: reason }),
        ).toContainText(String(count));
      }
    } else {
      await expect(
        page.getByTestId(`reason-table-${v.model_version}`),
      ).toHaveCount(0);
    }
  }
});

test("model versions are rendered separately rather than pooled", async ({ page }) => {
  // Stubbed, so the property holds on ANY database rather than only on one that
  // happens to carry two model versions. `reason_distribution.py` groups by
  // version because a distribution that silently mixes two describes neither;
  // this asserts the panel keeps them apart.
  await signInAsStaff(page, "admin");
  await page.route("**/fair-lending/reason-distribution", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        window: { since: "2026-01-01", until: "2026-08-30" },
        outcomes_counted: ["deny"],
        versions: [
          {
            model_version: "v-alpha",
            decisions: 3,
            distinct_reasons: 2,
            missing_reason: 1,
            reason_frequency: { insufficient_income: 2, thin_file: 1 },
          },
          {
            model_version: "v-beta",
            decisions: 1,
            distinct_reasons: 1,
            missing_reason: 0,
            reason_frequency: { high_debt_to_income: 1 },
          },
        ],
      }),
    }),
  );
  await page.goto("/admin");

  await expect(page.getByTestId("reason-version-v-alpha")).toBeVisible({
    timeout: 20_000,
  });
  await expect(page.getByTestId("reason-version-v-beta")).toBeVisible();

  // Each version's own figures, not a pooled total.
  await expect(page.getByTestId("reason-distinct-v-alpha")).toHaveText("2");
  await expect(page.getByTestId("reason-missing-v-alpha")).toHaveText("1");
  await expect(page.getByTestId("reason-distinct-v-beta")).toHaveText("1");
  await expect(page.getByTestId("reason-missing-v-beta")).toHaveText("0");

  // Reason rows land under their own version.
  await expect(
    page.getByTestId("reason-table-v-alpha").locator("tbody tr"),
  ).toHaveCount(2);
  await expect(
    page.getByTestId("reason-table-v-beta").locator("tbody tr"),
  ).toHaveCount(1);
  await expect(page.getByTestId("reason-table-v-beta")).toContainText(
    "high_debt_to_income",
  );
  await expect(page.getByTestId("reason-table-v-alpha")).not.toContainText(
    "high_debt_to_income",
  );

  // The stated window, echoed from the server rather than computed here.
  await expect(page.getByTestId("reason-monitoring-window")).toContainText(
    "2026-01-01",
  );
  await expect(page.getByTestId("reason-monitoring-window")).toContainText(
    "2026-08-30",
  );
});

test("the reporting window is stated, even when it is unbounded", async ({ page }) => {
  // A report that does not state its own window cannot be compared with
  // another one, and "all time" is a window worth saying out loud.
  await openAdmin(page);

  await expect(page.getByTestId("reason-monitoring-window")).toContainText(
    /reporting window/i,
  );
});

test("a failing reason-monitoring call does not blank the admin overview", async ({
  page,
}) => {
  // The split this section was built with. The portfolio load already puts two
  // calls under one `catch`; folding a third in would mean a governance panel
  // failing takes the applications and loans down with it.
  await signInAsStaff(page, "admin");
  await page.route("**/fair-lending/reason-distribution", (route) =>
    route.fulfill({ status: 500, contentType: "application/json", body: "{}" }),
  );
  await page.goto("/admin");

  await expect(page.getByTestId("reason-monitoring-error")).toBeVisible({
    timeout: 20_000,
  });
  // The rest of the page is still there.
  await expect(page.getByRole("heading", { name: /portfolio overview/i })).toBeVisible();
  await expect(page.getByRole("heading", { name: /recent applications/i })).toBeVisible();
  await expect(page.getByRole("heading", { name: /recent loans/i })).toBeVisible();
});

test("an empty distribution says so rather than rendering nothing", async ({ page }) => {
  await signInAsStaff(page, "admin");
  await page.route("**/fair-lending/reason-distribution", (route) =>
    route.fulfill({
      status: 200,
      contentType: "application/json",
      body: JSON.stringify({
        window: { since: null, until: null },
        outcomes_counted: ["deny"],
        versions: [],
      }),
    }),
  );
  await page.goto("/admin");

  await expect(page.getByTestId("reason-monitoring-empty")).toBeVisible({
    timeout: 20_000,
  });
});

test("a non-admin staff member cannot reach the admin overview", async ({ page }) => {
  // The page is admin-only and this change does not widen it. The ENDPOINT is
  // staff-gated server-side and untouched here; that boundary is owned by
  // origination-service's own tests.
  await signInAsStaff(page, "underwriter");
  await page.goto("/admin");

  await expect(page.getByTestId("reason-monitoring-heading")).toHaveCount(0);
  await expect(page.getByRole("heading", { name: /portfolio overview/i })).toHaveCount(0);
});
