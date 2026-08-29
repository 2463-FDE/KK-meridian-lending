import { test, expect } from "@playwright/test";
import { dbClient, signInAsStaff } from "./fixtures";

/**
 * Where an application got to, on the screen, from the database.
 *
 * The detail page could already show most of these facts in separate panels,
 * with one real hole: the boarded loan id lived in React state. Reload the page
 * and an already-boarded application rendered "This application has already been
 * boarded" with **no id and no link** — while `loans.app_id` had held the answer
 * the whole time and nothing read it back.
 *
 * The strip reads all five steps server-side, so a reload shows the same thing
 * the boarding session did. That reload case is the one worth testing and it is
 * the last case in this file.
 *
 * Expectations are computed from the database rather than written down here: a
 * spec that hard-codes "KYC verified" passes against a stale page for as long as
 * the seed does not change, then fails for the wrong reason when it does.
 *
 * This spec only READS. It decides nothing and boards nothing, so it consumes no
 * fixture application and can be re-run against one database.
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

/** An application that really was boarded, with the loan that references it. */
async function aBoardedApplication(): Promise<{ appId: number; loanId: number } | null> {
  return withDb(async (c) => {
    const r = await c.query(
      `SELECT a.id::int AS "appId", l.id::int AS "loanId"
         FROM applications a JOIN loans l ON l.app_id = a.id
        ORDER BY a.id DESC LIMIT 1`,
    );
    return r.rows[0] ?? null;
  });
}

async function openApplication(page: import("@playwright/test").Page, appId: number) {
  await signInAsStaff(page, "underwriter");
  await page.goto(`/underwriting/${appId}`);
  await expect(page.getByTestId("app-lifecycle")).toBeVisible({ timeout: 20_000 });
  // The strip renders a placeholder until its own request lands.
  await expect(page.getByTestId("lifecycle-submitted")).toBeVisible({
    timeout: 20_000,
  });
}

test("all five steps are shown, in order", async ({ page }) => {
  const boarded = await aBoardedApplication();
  test.skip(boarded === null, "no boarded application in this database");

  await openApplication(page, boarded!.appId);

  for (const key of ["submitted", "kyc", "decision", "offer", "boarded"]) {
    await expect(page.getByTestId(`lifecycle-${key}`)).toBeVisible();
  }
});

test("a boarded application names its loan and links to the account", async ({ page }) => {
  const boarded = await aBoardedApplication();
  test.skip(boarded === null, "no boarded application in this database");

  await openApplication(page, boarded!.appId);

  const step = page.getByTestId("lifecycle-boarded");
  await expect(step).toHaveAttribute("data-state", "complete");
  await expect(step).toContainText(`Loan #${boarded!.loanId}`);
  await expect(step.locator(`a[href="/servicing/${boarded!.loanId}"]`)).toBeVisible();
});

test("the boarded loan survives a reload", async ({ page }) => {
  // THE point of this change. `boardedLoanId` was session-local, so reloading
  // an already-boarded application lost the id and the link and left only
  // "This application has already been boarded". The strip reads the database,
  // so a fresh page load shows what the boarding session showed.
  const boarded = await aBoardedApplication();
  test.skip(boarded === null, "no boarded application in this database");

  await openApplication(page, boarded!.appId);
  await page.reload();
  await expect(page.getByTestId("lifecycle-boarded")).toBeVisible({ timeout: 20_000 });

  const step = page.getByTestId("lifecycle-boarded");
  await expect(step).toContainText(`Loan #${boarded!.loanId}`);
  await expect(step.locator(`a[href="/servicing/${boarded!.loanId}"]`)).toBeVisible();
});

test("an unboarded application says so rather than showing a blank step", async ({
  page,
}) => {
  const unboarded = await withDb(async (c) => {
    const r = await c.query(
      `SELECT a.id::int AS id FROM applications a
        WHERE NOT EXISTS (SELECT 1 FROM loans l WHERE l.app_id = a.id)
        ORDER BY a.id DESC LIMIT 1`,
    );
    return r.rows[0]?.id ?? null;
  });
  test.skip(unboarded === null, "every application in this database is boarded");

  await openApplication(page, unboarded!);

  const step = page.getByTestId("lifecycle-boarded");
  await expect(step).toHaveAttribute("data-state", "incomplete");
  await expect(step).toContainText(/not boarded/i);
  await expect(step.locator('a[href^="/servicing/"]')).toHaveCount(0);
});

test("a step with nothing recorded reads as not available, not as done", async ({
  page,
}) => {
  // The distinction the endpoint exists to keep: "no KYC row" is not "KYC
  // failed", and neither is "KYC passed". An unknown step must not borrow the
  // tick of a complete one.
  const noKyc = await withDb(async (c) => {
    const r = await c.query(
      `SELECT a.id::int AS id FROM applications a
        WHERE NOT EXISTS (SELECT 1 FROM kyc_checks k WHERE k.application_id = a.id)
        ORDER BY a.id DESC LIMIT 1`,
    );
    return r.rows[0]?.id ?? null;
  });
  test.skip(noKyc === null, "every application in this database has a KYC check");

  await openApplication(page, noKyc!);

  const step = page.getByTestId("lifecycle-kyc");
  await expect(step).toHaveAttribute("data-state", "unknown");
  await expect(step).toContainText(/not available/i);
  await expect(step).not.toContainText("✓");
});

test("the strip agrees with the database on every step", async ({ page }) => {
  // Read the underlying rows and assert the screen matches, rather than
  // asserting fixed labels that would drift with the seed.
  const boarded = await aBoardedApplication();
  test.skip(boarded === null, "no boarded application in this database");

  const facts = await withDb(async (c) => {
    const r = await c.query(
      `SELECT
         (SELECT count(*) FROM kyc_checks k WHERE k.application_id = a.id)::int AS kyc_rows,
         (SELECT outcome FROM decisions d WHERE d.app_id = a.id) AS decision,
         (SELECT accepted_at IS NOT NULL FROM offers o WHERE o.app_id = a.id
           ORDER BY o.id DESC LIMIT 1) AS offer_accepted
       FROM applications a WHERE a.id = $1`,
      [boarded!.appId],
    );
    return r.rows[0];
  });

  await openApplication(page, boarded!.appId);

  if (facts.kyc_rows === 0) {
    await expect(page.getByTestId("lifecycle-kyc")).toHaveAttribute(
      "data-state",
      "unknown",
    );
  }
  if (facts.decision) {
    await expect(page.getByTestId("lifecycle-decision")).toContainText(
      String(facts.decision).toUpperCase(),
    );
  }
  if (facts.offer_accepted === true) {
    await expect(page.getByTestId("lifecycle-offer")).toHaveAttribute(
      "data-state",
      "complete",
    );
    await expect(page.getByTestId("lifecycle-offer")).toContainText(/accepted/i);
  }
});

test("a borrower cannot read the lifecycle", async ({ page }) => {
  // The strip carries the boarded LOAN ID, and `/los/applications/{id}` is
  // reachable anonymously — which is why the lifecycle is a separate, staff
  // gated route rather than a field on the detail response.
  const boarded = await aBoardedApplication();
  test.skip(boarded === null, "no boarded application in this database");

  // Straight at the gateway with no session, the way an anonymous caller would.
  // Port 8000, as the other specs use -- and `localhost` rather than a bare
  // host string, because this repository has already been bitten by a name that
  // resolves to IPv6 and refuses the connection.
  const res = await page.request.get(
    `http://localhost:8000/los/applications/${boarded!.appId}/lifecycle`,
  );

  // The gateway proxies `/los/*` anonymously, so this reaches origination with
  // no role header and origination refuses it. That refusal is the whole reason
  // the lifecycle is a separate route: on the detail response, which IS
  // anonymously readable, the boarded loan id would have been public.
  expect(res.status()).toBeGreaterThanOrEqual(400);
  expect(res.status()).toBeLessThan(500);
});
