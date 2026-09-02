import { Page, expect } from "@playwright/test";
import { Client } from "pg";

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

/** Seeded staff logins (db/init/002_seed.sql) -- demo credentials in a local
 * training repo, never real. */
export async function signInAsStaff(page: Page, username = "underwriter"): Promise<void> {
  await page.goto("/login");
  await page.locator("#username").fill(username);
  await page.locator("#password").fill("password");
  await page.getByRole("button", { name: /Sign in/ }).click();
  // The app redirects away from /login once the session is established.
  await expect(page).not.toHaveURL(/\/login$/, { timeout: 15_000 });
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
  await page.goto("/login");
  await page.locator("#username").fill(username);
  await page.locator("#password").fill("password");
  await page.getByRole("button", { name: /Sign in/ }).click();
  await expect(page).not.toHaveURL(/\/login$/, { timeout: 15_000 });
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
 * requiring all four or none.
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
     VALUES ($1, 12000.00, 7.99, 36, 375.94, 35, 375.90, 'B1', 'current')
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
