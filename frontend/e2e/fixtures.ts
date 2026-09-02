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
 * hop of the trail. See e2e/README.md for what writes what.
 *
 * The append-only consequence is unchanged and its handling is not: a
 * `ledger_entries` insert cannot be undone, so a spec that writes one takes a
 * loan of its own rather than reusing a seeded one. It used to take that loan
 * from a finite reserved band and consume it (RF-27); it now creates one --
 * `createFixtureLoan` below. */
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

  // 6. Keep the PROVEN session across later document loads.
  //
  // WHY THIS IS NEEDED, and measured rather than assumed. Everything above
  // proves the session exists and the gateway honours it. What it cannot prove
  // is that the `localStorage` write survives the next FULL document load: the
  // app writes the session and navigates in the same tick, and a spec's
  // following `page.goto(...)` is a fresh document. When that write has not yet
  // reached the storage backend the new document starts empty, `RequireRole`
  // sees no cached user, and the browser is sent to `/login` -- with no
  // `/auth/me` call, so nothing in any log explains it. Across a full run the
  // gateway logged 1811 `/auth/me` requests, every one 200, 601 logins all 200,
  // zero 429 and zero 5xx, while specs still landed on the login page.
  //
  // A/B on two freshly reseeded stacks, whole suite:
  //   with this block     216 passed,  0 failed,  3.3 min
  //   without this block  196 passed, 20 failed, 12.0 min
  //
  // Re-applying the SAME token the real login produced makes the session
  // deterministic for later navigations. It does not fabricate authentication:
  // the credentials went to the real gateway, the real token came back, and
  // `/auth/me` has already confirmed the account and role -- if any of that had
  // failed this line is never reached. What it removes is a dependency on
  // browser storage-flush timing, which no spec here is trying to assert.
  //
  // Later sign-ins are not clobbered: init scripts run in the order added, so
  // the most recent sign-in's script writes last and wins, which is what a
  // reader expects from "who is signed in now".
  const rawUser = JSON.stringify(stored.user);
  await page.context().addInitScript(
    ([tokenKey, userKey, token, user]) => {
      try {
        window.localStorage.setItem(tokenKey, token);
        window.localStorage.setItem(userKey, user);
      } catch {
        /* private mode or blocked site data -- the assertions above still ran */
      }
    },
    [SESSION_KEYS.token, SESSION_KEYS.user, stored.token, rawUser] as const,
  );
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

/**
 * A serviced loan created FOR ONE TEST, and retired when the file is done.
 *
 * WHY THIS REPLACES THE RESERVED BAND (RF-27). `fee-waiver-clarity.spec.ts` and
 * `approvals-resolved-history.spec.ts` each wrote a permanent `ledger_entries`
 * row against a seeded loan, so neither could reuse one: the ledger is
 * append-only and a loan it has assessed a fee against cannot truthfully become
 * untouched again. They took an untouched loan from a band past the ids the rest
 * of the suite reaches and CONSUMED it -- about fifteen repeated local runs
 * against the same persistent database exhausted the band and the specs failed
 * with `no untouched serviced loan left in the reserved band -- reseed the
 * database`. The finiteness was the defect: the fixture depended on a supply
 * every run drew down.
 *
 * A loan created per test has no supply to exhaust, so the specs are repeatable
 * without reseeding and without retries, sleeps, a wider OFFSET or randomness --
 * none of which would have fixed it, because the band still runs out.
 *
 * WHAT MAKES THE DIRECT `balances` WRITE HONEST, which is the part RF-27 warned
 * about: "Do NOT restore the previous save/restore fixture -- it wrote
 * `balances.past_due` directly ... and its restore rewrote history the ledger
 * had already recorded." The objection was to the RESTORE rewriting recorded
 * history, not to the insert. Nothing here restores anything, and the insert
 * does not bypass the ledger: `balances_capture_legacy_delta` (db/init/007)
 * fires on it and writes the matching `legacy_direct_write` entry, so
 * `balances_ledger_parity` -- a DEFERRABLE INITIALLY DEFERRED constraint trigger
 * that fires on INSERT -- holds at commit. Verified against a running database
 * rather than read off the schema.
 *
 * `pastDue` is 0 for that reason and it is not incidental. A non-zero seed
 * writes a `legacy_direct_write` fees entry of its own, so a later
 * `fee_assessed` of 350 leaves 375 owed, not 350 -- measured, after a fixture
 * that asserted an exact fee balance would have been wrong by the seed. Callers
 * that need fees put them on through the ledger entry that justifies them.
 *
 * The four contract columns are supplied together because `loans` has a CHECK
 * requiring all four or none, and the amounts are the ones servicing's own
 * generator produces for `12000.00 @ 7.99% / 36` -- NOT numbers chosen to look
 * plausible.
 *
 * That distinction was a real defect, found by this audit rather than by a test.
 * The first version of this fixture carried `375.94 / 375.90`, which does not
 * amortize 12,000: the closing balance lands at 1.72, and `GET
 * /loans/{id}/schedule` correctly answers "This loan's recorded terms do not add
 * up ... 1.72 remains unaccounted for". Sixty-two fixture loans in the local
 * database carried it. Every one was a synthetic loan tripping a warning built
 * to report a genuine data defect, and while a run is in flight those loans are
 * `current`, so a demo landing on one is shown that warning about a loan the
 * test suite manufactured.
 *
 * `db/tests/test_fixture_contracts_amortize.py` now checks these amounts against
 * the generator, because the generator lives in Python and these numbers live in
 * TypeScript -- so the only way this stays true is a guard that reads both.
 */
export interface FixtureLoan {
  loanId: number;
  applicantName: string;
  balance: number;
}

/** Distinguishes this process's rows from a concurrent or earlier run's, so
 * teardown retires only what it created. */
const FIXTURE_RUN = `${Date.now().toString(36)}-${process.pid}`;

export function fixtureLoanPrefix(label: string): string {
  return `E2E ${label} ${FIXTURE_RUN}`;
}

let fixtureLoanSeq = 0;

export async function createFixtureLoan(
  client: Client,
  label: string,
  balance = 11_950.0,
): Promise<FixtureLoan> {
  const applicantName = `${fixtureLoanPrefix(label)} #${++fixtureLoanSeq}`;
  const inserted = await client.query(
    `INSERT INTO loans (applicant_name, principal, note_rate_pct, term_months,
                        regular_payment, regular_payment_count, final_payment,
                        schedule_version, status)
     VALUES ($1, 12000.00, 7.99, 36, 375.98, 35, 376.03, 'B1', 'current')
     RETURNING id`,
    [applicantName],
  );
  const loanId = Number(inserted.rows[0].id);
  await client.query(
    "INSERT INTO balances (loan_id, balance, past_due) VALUES ($1, $2, 0.00)",
    [loanId, balance],
  );
  return { loanId, applicantName, balance };
}


/**
 * Take this file's fixture loans out of the serviced portfolio.
 *
 * RETIRED, not deleted, and the difference is not tidiness: `balances` refuses a
 * DELETE outright -- "balances rows cannot be deleted during ledger cutover" --
 * so removing the row is not available, and deleting the loan while its balances
 * row survived would leave an orphan. Closing it takes it out of the serviced
 * portfolio, which is the only property any other spec cares about: they all
 * select `l.status = 'current'`.
 *
 * Scoped to this process's prefix, so a run that crashed before teardown leaves
 * traceable rows behind rather than this one closing loans it did not create.
 */
export async function retireFixtureLoans(client: Client, label: string): Promise<void> {
  await client.query("UPDATE loans SET status = 'closed' WHERE applicant_name LIKE $1",
                     [`${fixtureLoanPrefix(label)}%`]);
}

/**
 * A serviced loan the SEEDED BORROWER owns, created for one spec file (RF-30).
 *
 * WHY THIS EXISTS, and why `createFixtureLoan` above could not be reused. The
 * borrower payment specs pay through the real UI, and a payment permanently
 * reduces the loan's balance. They all pointed at `SEEDED_BORROWER.loanId`, so
 * every run drew that balance down and never gave it back -- the ledger is
 * append-only, and D14 refuses an overpayment, so once the balance approaches
 * zero those specs fail on a refusal that is correct.
 *
 * Measured on a FRESH volume rather than inferred: `db/init` seeds `4471` at
 * `12200.00`; after roughly one and a half full runs it stood at `11169.73`.
 * That is about **1030 per full run**, so **eleven or twelve runs** exhaust it.
 * RF-27's reserved band lasted about fifteen. Same pattern, same order of
 * magnitude -- which is why this is a fix rather than a note. An earlier draft
 * of the RF-30 row said the specs had "room for many more sessions"; that was
 * based on a drain measured across an already-mutated database and it
 * understated the defect.
 *
 * `createFixtureLoan` does not transfer, because a borrower's access is not a
 * property of the loan. `gateway/app/main.py::_borrower_loans` resolves it as
 * `loans.app_id -> applications.applicant_id = users.applicant_id`, so a loan
 * with no application, or one whose application belongs to somebody else, is
 * invisible to the borrower however correct its balances are. This creates the
 * application too, owned by the signed-in borrower's applicant.
 *
 * RETIREMENT IS DIFFERENT HERE TOO, and this is the part worth reading.
 * `_borrower_loans` applies **no status filter** -- it lists every loan for the
 * applicant. So closing the loan, which is all `retireFixtureLoans` does and is
 * enough for every staff spec, would leave it in the borrower's own portfolio
 * for ever and the list would grow without bound on every run: the same defect
 * in a different column. Teardown therefore also points the fixture APPLICATION
 * at a dedicated synthetic holder, so the loan leaves the borrower's portfolio.
 *
 * That is a deliberate trade and not a tidy one: it rewrites who applied. It is
 * acceptable only because these rows are fixtures with a traceable name and
 * nothing reads the reassigned application afterwards -- and the alternative,
 * deleting, is not available: `balances` refuses a DELETE outright during ledger
 * cutover, and removing the loan while its balances row survived would leave an
 * orphan. Neither is topping the balance back up: that is a ledger write, and
 * inventing money to make a fixture reusable is exactly the history-rewriting
 * RF-27 forbade.
 */
export interface BorrowerFixtureLoan {
  loanId: number;
  appId: number;
  applicantName: string;
  balance: number;
}

/** Where a retired borrower fixture loan is parked. Named so a stray row says
 * what it is. */
const RETIRED_FIXTURE_HOLDER = "E2E Retired Fixture Holder";

export async function createBorrowerLoan(
  client: Client,
  label: string,
  balance = 12_000.0,
): Promise<BorrowerFixtureLoan> {
  // The borrower's applicant, read rather than hard-coded: the seed's ids are
  // the seed's business, and a spec that pinned `1` here would break silently
  // the day the seed renumbered.
  const who = await client.query(
    "SELECT applicant_id FROM users WHERE username = $1", [SEEDED_BORROWER.username]);
  const applicantId = who.rows[0]?.applicant_id;
  if (!applicantId) {
    throw new Error(
      `no applicant_id on user ${SEEDED_BORROWER.username}; a borrower loan cannot ` +
      "be owned by anyone, so this fixture must fail rather than create an " +
      "invisible loan");
  }

  const applicantName = `${fixtureLoanPrefix(label)} #${++fixtureLoanSeq}`;

  const app = await client.query(
    `INSERT INTO applications (applicant_id, amount, term_months, purpose,
                               income, status)
     VALUES ($1, 12000.00, 36, 'personal', 60000, 'funded')
     RETURNING id`,
    [applicantId],
  );
  const appId = Number(app.rows[0].id);

  // Same amortizing contract as `createFixtureLoan`, and for the same reason:
  // `375.98 x 35` then `376.03` closes 12,000 at 0.00, so the loan does not
  // trip the schedule route's data-defect warning.
  // `db/tests/test_fixture_contracts_amortize.py` checks these numbers against
  // servicing's own generator.
  const loan = await client.query(
    `INSERT INTO loans (app_id, applicant_name, principal, note_rate_pct,
                        term_months, regular_payment, regular_payment_count,
                        final_payment, schedule_version, status)
     VALUES ($1, $2, 12000.00, 7.99, 36, 375.98, 35, 376.03, 'B1', 'current')
     RETURNING id`,
    [appId, applicantName],
  );
  const loanId = Number(loan.rows[0].id);

  // `past_due` 0 deliberately -- see `createFixtureLoan`: a non-zero seed
  // writes a `legacy_direct_write` fees entry of its own.
  await client.query(
    "INSERT INTO balances (loan_id, balance, past_due) VALUES ($1, $2, 0.00)",
    [loanId, balance],
  );

  return { loanId, appId, applicantName, balance };
}

export async function retireBorrowerLoans(client: Client, label: string): Promise<void> {
  const prefix = `${fixtureLoanPrefix(label)}%`;

  // One holder row, reused. `applicants` has no unique constraint on name, so
  // this reads before it writes rather than relying on ON CONFLICT.
  const existing = await client.query(
    "SELECT id FROM applicants WHERE name = $1 LIMIT 1", [RETIRED_FIXTURE_HOLDER]);
  let holderId = existing.rows[0]?.id;
  if (!holderId) {
    const made = await client.query(
      "INSERT INTO applicants (name, is_entity) VALUES ($1, FALSE) RETURNING id",
      [RETIRED_FIXTURE_HOLDER]);
    holderId = made.rows[0].id;
  }

  // Out of the borrower's portfolio first, then out of the serviced one.
  await client.query(
    `UPDATE applications SET applicant_id = $1
      WHERE id IN (SELECT app_id FROM loans WHERE applicant_name LIKE $2)`,
    [holderId, prefix]);
  await client.query(
    "UPDATE loans SET status = 'closed' WHERE applicant_name LIKE $1", [prefix]);
}
