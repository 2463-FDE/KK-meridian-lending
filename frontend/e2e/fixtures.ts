import { Page, expect } from "@playwright/test";
import { Client } from "pg";
import { GATEWAY_URL, SESSION_KEYS, roleHome } from "../lib/api";

/** Fictional test data only -- no real/production-like SSNs or card data.
 * The last SSN digit is fixed (even -> approve band, odd -> deny/refer
 * band, matching decision-service's stub scoring: bureau_score = 680 if
 * even else 612) while the rest of the number is derived from the current
 * timestamp so repeated runs never collide or depend on a prior run's
 * application id. */
let _seq = 0;
export function uniqueDigits(len: number): string {
  // A per-process counter FIRST, then the timestamp.
  //
  // This used to be the last `len` digits of Date.now() alone, and
  // fictionalApplicant() builds its SSN from characters 0-4 of the result --
  // which, for a millisecond timestamp, are the digits that change only every
  // ~10 seconds. Two applications created inside the same window therefore got
  // the SAME SSN, and the second one's submission failed: the wizard never
  // reached Step 5 and the failure surfaced in submitApplication(), far from
  // its cause.
  //
  // Latent until the suite grew. It began failing when this branch added
  // specs, and which spec failed varied from run to run depending on where the
  // 10-second boundary happened to fall -- flakiness that looked like
  // parallelism but is not (playwright.config.ts sets workers: 1).
  //
  // Putting the counter in the leading digits guarantees every call differs in
  // exactly the characters the SSN is cut from.
  //
  // **The counter alone is per PROCESS, which is not the same as unique** (RF-24).
  // `_seq` is module scope, so it starts at 0 in every Playwright worker. Under
  // `workers: 1` that is genuinely collision-free, which is why the comment above
  // could end where it did. Run the suite with four workers and four processes
  // each begin at `000` -- from then on uniqueness rests entirely on the six
  // timestamp digits differing, which is timing luck rather than a guarantee.
  // Two workers calling this inside the same millisecond window produce the same
  // SSN, and the failure surfaces exactly where the old one did: a wizard that
  // never reaches Step 5, in whichever spec lost the race.
  //
  // So the worker index goes in front of the counter. `TEST_WORKER_INDEX` is set
  // by Playwright per worker process and is absent when the file is run outside
  // it, where 0 is correct because there is only one process. Taken modulo 10 to
  // stay one digit: beyond ten workers two of them share a leading digit and the
  // guarantee degrades back to the counter plus timestamp, which is the current
  // behaviour rather than a regression.
  // Layout: worker(1) + counter(3) + timestamp(5) = 9, the length
  // `fictionalApplicant` asks for. The counter keeps all THREE of its digits.
  //
  // Review finding B1: the first version of this fix took the room for the
  // worker digit out of the counter, leaving two digits. That reintroduced the
  // original defect at a tenth of the old scale -- the 101st call in one worker
  // inside the same timestamp bucket reuses an SSN, demonstrated at exactly
  // `dupAt: 100`. Buying cross-worker uniqueness with a 10x cut to within-worker
  // uniqueness is not a fix, and "ample" was an assumption I had not measured.
  //
  // The digit comes out of the TIMESTAMP instead, which costs nothing that
  // matters: the counter is what guarantees uniqueness within a worker, and the
  // timestamp only has to break ties between processes that happen to be at the
  // same counter. Five digits still distinguish those to 100 seconds.
  //
  // Every one of the SSN's source characters is now meaningful: it is cut from
  // `d.slice(0, 5)`, which is the worker digit plus the whole counter plus one
  // timestamp digit.
  const worker = (Number(process.env.TEST_WORKER_INDEX ?? 0) % 10).toString();
  const counter = (_seq++ % 1000).toString().padStart(3, "0");
  const stamp = Date.now().toString().slice(-5);
  return (worker + counter + stamp).slice(-len).padStart(len, "0");
}

export interface FictionalApplicant {
  name: string;
  ssn: string;
  email: string;
  phone: string;
  income: number;
}

export function fictionalApplicant(label: string, lastDigitEven: boolean, income: number): FictionalApplicant {
  const d = uniqueDigits(9);
  const lastDigit = lastDigitEven ? "0" : "1";
  const ssn = `999-${d.slice(0, 2)}-${d.slice(2, 5)}${lastDigit}`;
  const phone = `(555) 0${d.slice(5, 7)}-${d.slice(2, 6)}`;
  return {
    name: `${label} Fictional`,
    ssn,
    email: `${label.toLowerCase()}.${d}@example.test`,
    phone,
    income,
  };
}

export async function submitApplication(
  page: Page,
  applicant: FictionalApplicant,
  opts?: { stopAtReview?: boolean },
): Promise<void> {
  await page.goto("/apply");
  await expect(page.getByText("Step 1 of 5")).toBeVisible();
  await page.getByPlaceholder("Jane Q. Borrower").fill(applicant.name);
  await page.locator('input[type="date"]').fill("1990-01-01");
  await page.getByPlaceholder("123-45-6789").fill(applicant.ssn);
  await page.getByPlaceholder("you@example.com").fill(applicant.email);
  await page.getByPlaceholder("(555) 555-0123").fill(applicant.phone);
  await page.getByPlaceholder("123 Main St").fill("1 Fictional Ave");
  await page.getByPlaceholder("Springfield").fill("Springfield");
  await page.locator("select").first().selectOption("IL");
  await page.getByPlaceholder("62704").fill("62704");
  await page.getByRole("button", { name: "Next" }).click();

  await expect(page.getByText("Step 2 of 5")).toBeVisible();
  const plainInputs = page.locator('main input:visible:not([placeholder]):not([type="range"])');
  await plainInputs.nth(0).fill("Fictional Testing Co");
  await plainInputs.nth(1).fill("QA Analyst");
  await page.getByPlaceholder("65000").fill(String(applicant.income));
  await page.getByPlaceholder("3").fill("3");
  await page.getByRole("button", { name: "Next" }).click();

  await expect(page.getByText("Step 3 of 5")).toBeVisible();
  await page.getByRole("button", { name: "Next" }).click();

  await expect(page.getByText("Step 4 of 5")).toBeVisible();

  // `stopAtReview` leaves the wizard on the review screen without submitting.
  // submit-in-flight-edit.spec.ts needs to press Submit itself, because the
  // whole point of that test is what happens DURING the request -- a helper
  // that submits and waits for Step 5 has already skipped the window.
  if (opts?.stopAtReview) return;

  await page.getByRole("button", { name: "Submit application" }).click();

  await expect(page.getByText("Step 5 of 5")).toBeVisible({ timeout: 15_000 });
  await expect(page.getByText("received")).toBeVisible({ timeout: 15_000 });
}

export async function currentAppId(page: Page): Promise<string> {
  const text = await page.locator(".alert-info").innerText();
  const m = text.match(/#(\d+)/);
  if (!m) throw new Error(`could not find an application id in: ${text}`);
  return m[1];
}

export async function getDecision(page: Page): Promise<void> {
  await page.getByRole("button", { name: /Get decision/ }).click();
  await expect(page.getByText("Underwriting decision")).toBeVisible({ timeout: 15_000 });
}

/** Postgres, used to verify test invariants AND to write fixture state (never
 * used by application runtime code). Connects using the same DATABASE_URL the
 * backend services use -- required, not defaulted to a real credential.
 *
 * This said "read-only, used only to verify test invariants" and was wrong:
 * nine spec files write through this client. It mattered more than the wording
 * suggests, because README.md points a reader here for the DATABASE_URL
 * contract -- so the one sentence that reasserted the false claim was the last
 * hop of the trail. See e2e/README.md for what writes what, and RF-27 in
 * docs/DEBT.md for the append-only consequence: a `ledger_entries` insert
 * cannot be undone, so `fee-waiver-clarity` consumes a loan per test. */
export function dbClient(): Client {
  const url = process.env.DATABASE_URL;
  if (!url) {
    throw new Error("DATABASE_URL is required to run these E2E tests (see e2e/README.md)");
  }
  return new Client({ connectionString: url });
}

export async function countRows(client: Client, table: string, whereCol: string, appId: string): Promise<number> {
  const res = await client.query(`SELECT count(*)::int AS n FROM ${table} WHERE ${whereCol} = $1`, [appId]);
  return res.rows[0].n;
}

/** Income that lands an odd-SSN applicant in the manual-review (REFER) band.
 * decision-service's stub model score is int(bureau_score * 0.9 + income/1000);
 * an odd last SSN digit gives bureau_score 612, so 612*0.9 + 60 = 610 -- above
 * the 600 deny cutoff and below the 660 approve cutoff (see
 * decision-service/app/decision.py::_run_model). */
export const REFER_BAND_INCOME = 60_000;

/**
 * Sign in, and do not return until the browser is PROVABLY authenticated.
 *
 * THE DEFECT THIS REPLACES. Both helpers used to end at
 * `expect(page).not.toHaveURL(/\/login$/)`. That is a proxy for "signed in",
 * and it is a proxy that comes true too early: the address can stop ending in
 * `/login` while `localStorage` is still empty and `RequireRole` has not yet
 * completed its `/auth/me` check. The next `page.goto(...)` then renders a
 * role-gated screen with no session, the guard sends the browser back to
 * `/login`, and the spec fails somewhere with no visible relationship to
 * signing in -- a nav-link count, a missing approvals card, a heading that
 * never appears. Every failure looks like a different bug.
 *
 * Both of PR #160's remaining CI failures were this: the artifacts show the
 * LOGIN PAGE at assertion time, and the "2 nav links" the appbar case saw are
 * the anonymous Apply / Log in pair.
 *
 * WHAT IS PROVEN HERE, in order, each one a thing a later step depends on:
 *
 *   1. no visible authentication error -- fail immediately rather than after a
 *      15s wait for something that is never going to appear;
 *   2. the session is readable in the browser, under the app's OWN keys
 *      (`SESSION_KEYS`, imported rather than re-typed);
 *   3. the stored user is the account that was asked for;
 *   4. the gateway agrees -- `/auth/me` returns that same account and role,
 *      which is the check `RequireRole` is about to make on every guarded page;
 *   5. the app has landed on that role's home (`roleHome`, imported, so the
 *      suite cannot drift from the app's own routing), and is not on /login.
 *
 * This is STRICTER than what it replaces, not looser. The original URL
 * assertion is still made, at step 5. Nothing here retries, sleeps, or widens a
 * timeout.
 */
async function signInAndProveIt(page: Page, username: string): Promise<void> {
  await page.goto("/login");
  await page.locator("#username").fill(username);
  await page.locator("#password").fill("password");
  await page.getByRole("button", { name: /Sign in/ }).click();

  // 1. A visible failure is final -- waiting on it would only slow the report.
  const authError = page.locator(".alert-error");
  await expect
    .poll(async () => (await authError.count()) > 0 ? await authError.innerText() : null,
          { timeout: 15_000, message: "sign-in reported an error" })
    .toBeNull()
    .catch(async () => {
      throw new Error(
        `sign-in as ${username} failed: ${(await authError.innerText()).trim()}`);
    });

  // 2 + 3. The session exists in the browser, and it is the right account.
  await page.waitForFunction(
    ([tokenKey, userKey, expected]) => {
      try {
        const token = window.localStorage.getItem(tokenKey);
        const raw = window.localStorage.getItem(userKey);
        if (!token || !raw) return false;
        return (JSON.parse(raw) as { username?: string }).username === expected;
      } catch {
        return false;
      }
    },
    [SESSION_KEYS.token, SESSION_KEYS.user, username] as const,
    { timeout: 15_000 },
  );

  const stored = await page.evaluate(
    ([tokenKey, userKey]) => ({
      token: window.localStorage.getItem(tokenKey) as string,
      user: JSON.parse(window.localStorage.getItem(userKey) as string) as
        { username: string; role: string },
    }),
    [SESSION_KEYS.token, SESSION_KEYS.user] as const,
  );

  // 4. The gateway agrees. This is the same question RequireRole asks, so a
  // session the guard would reject fails here instead of three steps later.
  // The token goes in a header and is never logged; only the status is.
  const me = await page.request.get(`${GATEWAY_URL}/auth/me`, {
    headers: { Authorization: `Bearer ${stored.token}` },
  });
  if (!me.ok()) {
    throw new Error(
      `/auth/me refused a session that had just been created for ${username}: ` +
      `HTTP ${me.status()} (${categoriseAuthFailure(me.status())})`);
  }
  const body = (await me.json()) as { username: string; role: string };
  expect(body.username, "/auth/me returned a different account").toBe(username);
  expect(body.role, "/auth/me returned a different role").toBe(stored.user.role);

  // 5. Landed where this role belongs, and off the login page.
  await expect(page).toHaveURL(new RegExp(`${roleHome(body.role)}/?$`), { timeout: 15_000 });
  await expect(page).not.toHaveURL(/\/login$/, { timeout: 15_000 });
}

/** Name the failure class without printing anything sensitive. */
function categoriseAuthFailure(status: number): string {
  if (status === 401 || status === 403) return "credentials rejected";
  if (status === 429) return "rate limited";
  if (status >= 500) return "gateway error";
  return "unexpected";
}

/** Seeded staff logins (db/init/002_seed.sql) -- demo credentials in a local
 * training repo, never real. */
export async function signInAsStaff(page: Page, username = "underwriter"): Promise<void> {
  await signInAndProveIt(page, username);
}

/** The seeded borrower (db/init/002_seed.sql): `maria` owns applicant 1,
 * application 4471 and loan 4471. Same demo password as the staff logins, same
 * local-training-repo caveat -- never a real credential. */
export const SEEDED_BORROWER = { username: "maria", loanId: 4471 } as const;

/** Sign in as the seeded borrower.
 *
 * Separate from `signInAsStaff` on purpose rather than passing "maria" to it:
 * the two land on different role homes, and a borrower spec that silently
 * depended on a staff helper would stop proving borrower access the moment the
 * helper grew a staff-only step.
 */
export async function signInAsBorrower(
  page: Page,
  username: string = SEEDED_BORROWER.username,
): Promise<void> {
  await signInAndProveIt(page, username);
}

/** Resolve a REFER from the staff underwriting screen. `reason` is required by
 * the UI -- the Record button stays disabled until it is non-empty, which the
 * callers assert. */
export async function resolveReferAsStaff(
  page: Page,
  appId: string,
  outcome: "approve" | "deny",
  reason: string,
): Promise<void> {
  await page.goto(`/underwriting/${appId}`);
  await expect(page.getByText(/manual-review band/i)).toBeVisible({ timeout: 15_000 });

  // SCOPED TO THE MANUAL-REVIEW CARD. This used to reach for `page.locator(
  // "textarea")` -- "the only textarea on the page" -- which was true until
  // RF-25's manual DTI panel put a second one on the same screen, and four
  // specs then failed on a strict-mode violation while the decision control
  // they were driving still worked perfectly. The card is found by the sentence
  // only it carries, so a future panel can add a third textarea without this
  // reaching for it.
  const reviewCard = page
    .locator(".card")
    .filter({ hasText: /manual-review band/i })
    .first();

  const record = reviewCard.getByRole("button", { name: /^Record (approval|denial)$/ });
  // A reason is mandatory: the control is disabled until one is typed.
  await expect(record).toBeDisabled();

  await reviewCard.locator("select").filter({ hasText: "Approve" }).first().selectOption(outcome);
  await reviewCard.locator("textarea").fill(reason);

  await expect(record).toBeEnabled();
  await record.click();
  await expect(page.getByText("Decision finalized")).toBeVisible({ timeout: 15_000 });
}
