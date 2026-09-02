# Borrower-workflow E2E (Playwright)

Committed, repeatable replacement for the interactive browser verification
done during the offer-workflow/token-security audit -- that verification
was never checked into the repository and was not independently
repeatable. These tests are.

## Running locally

1. Start the full backend stack + Postgres (from the repo root):
   `docker compose up -d`
2. Apply migrations through `0022` against that Postgres if not already
   current (see `db/migrations/`).
3. From `frontend/`:
   ```
   DATABASE_URL=postgresql://meridian:postgres@localhost:5432/meridian npm run test:e2e
   ```

## Required environment variables

- `DATABASE_URL` -- required. Same Postgres the backend services use.
  Mostly used to assert test invariants ("exactly one
  application/decision/offer/loan"), but **the suite also writes fixture
  state** through this connection. Point it at a database you are willing to
  have modified. No default is committed; the suite fails fast with a clear
  error if this is unset (see `fixtures.ts::dbClient`).

  This paragraph used to say the connection was "used read-only ... never
  written to by these tests". That was false for **nine** spec files, and the
  correction matters because a reader who trusts it draws the wrong conclusion
  when the suite behaves as though state carried over -- which it does.

  Rather than trust this list, re-derive it. Note the second filter and the
  absence of `-o`: `grep -o` prints only the matched SQL, which throws away the
  `//` or `*` that marks a comment, so a command built on it cannot tell code
  from prose and over-reports by two.

  ```
  grep -rnE '(INSERT INTO|UPDATE|DELETE FROM) [a-z_]+' frontend/e2e/*.ts \
    | grep -vE ':[0-9]+: *(\*|//|/\*)'
  ```
  As of this commit that is:

  | Spec | Writes |
  |---|---|
  | `fee-waiver-clarity` | `INSERT ledger_entries` (a `fee_assessed` entry) |
  | `servicing-raises-a-proposal` | `INSERT ledger_entries` |
  | `approval-queue-self-approval` | `INSERT pending_movements` |
  | `approvals-resolved-history` | `INSERT pending_movements` |
  | `payment-allocation` | `INSERT` / `DELETE payments` |
  | `reconciliation-review-queue` | `INSERT` / `DELETE payments`, `INSERT` / `DELETE reconciliation_review_items` |
  | `amount-financed-breakdown` | `UPDATE offers` |
  | `offer-disclosure-ui` | `UPDATE offers` |
  | `regeneration-reprices-the-offer` | `UPDATE offers` |
  | `reconstructed-schedule-warning` | `UPDATE loans` |

  Ten specs, and one shared HELPER. The helper is kept out of the table above
  deliberately: it is not a spec, and its writes belong to whichever test calls
  it.

  | Helper | Writes |
  |---|---|
  | `fixtures.ts` (`createFixtureLoan`, `retireFixtureLoans`) | `INSERT loans`, `INSERT balances`, `UPDATE loans` (the retire) |

  (Two matches the naive grep reports are prose, not code: `UPDATE balances` in
  `fee-waiver-clarity`, describing what the fixture used to do before PR #113
  removed the direct write to the projection, and `UPDATE decisions` in
  `decision-evidence`, in a comment explaining what a staff override does.
  Neither spec performs that write, which is why the command above is filtered.)

  This table has now been wrong twice, in the same way, which is why it is no
  longer maintained by hand alone.

  A first version said "seven" and omitted `servicing-raises-a-proposal` and
  `regeneration-reprices-the-offer`, because the command it came from was
  truncated with `head`. The second omitted `approvals-resolved-history` --
  which the paragraphs below already discussed as an append-only writer -- and
  the `fixtures.ts` writes this suite's fixture helper had just gained. An
  inventory presented as complete and quietly missing entries is the same
  wrong-model failure this section exists to remove, and giving the re-derive
  command was evidently not enough on its own.

  `db/tests/test_e2e_write_inventory_is_complete.py` re-derives the set and
  fails when this table and the tree disagree, so a third omission is a red
  build rather than a paragraph a reader trusts.

  **Two of those writes are append-only and cannot be undone**, both
  `ledger_entries` inserts. Everything else is set up and torn down within a
  test. So `fee-waiver-clarity` and `approvals-resolved-history` do not reuse a
  seeded loan and do not restore one either -- a loan they have assessed a fee
  against cannot truthfully become untouched again. Each **creates** the loan it
  needs (`createFixtureLoan` in `fixtures.ts`) and closes it in `afterAll`.

  They used to take an untouched loan from a reserved band past the ids the rest
  of the suite reaches, and consumed one per test: repeated local runs against a
  single database exhausted the band and the specs failed with `no untouched
  serviced loan left in the reserved band -- reseed the database`, whose remedy
  was `docker compose down -v`. That was **RF-27** in
  [`docs/DEBT.md`](../../docs/DEBT.md), now closed -- the fixture no longer draws
  on a finite supply, so there is nothing to exhaust and no reseed to remember.
  CI was never affected either way, because every run starts from a fresh
  volume.
- `E2E_BASE_URL` -- optional, defaults to `http://localhost:3000`
  (the frontend's own dev/prod server).

## What's covered

- `approved-workflow.spec.ts` -- full apply -> automated approval -> the
  auto-generated offer displays without a duplicate-create error -> accept
  -> board -> exactly one application/decision/offer/loan in Postgres.
- `denied-workflow.spec.ts` -- a deterministically denied application shows
  its reason, offers no "View your offer" action, and creates neither an
  offer nor a loan.
- `existing-offer.spec.ts` -- arranges an offer that's already
  auto-generated in Postgres before the browser ever asks for it, then
  proves the browser retrieves it (GET) rather than attempting to create a
  second one (asserted directly against network traffic, not inferred from
  the UI alone).

## Test data

All identities are fictional, generated fresh per run from the current
timestamp (see `fixtures.ts::fictionalApplicant`) -- no fixed
production-like SSN, no hardcoded application id from a previous run.
Approve/deny outcomes are steered deterministically via decision-service's
own stub scoring rule (even-ending SSN -> higher bureau score; odd -> lower)
combined with income, not by hoping a shared fixture still scores the same
way.

## Diagnostics and safety

- Playwright traces are **off** (`playwright.config.ts`) -- a trace records
  full request/response detail, including the `X-Offer-Accept-Token`
  header this suite exercises; not producing traces at all is the simplest
  way to guarantee that credential never ends up in a committed or
  uploaded artifact.
- Screenshots are captured **only on failure**. The raw token is never
  rendered as visible text on any page (it exists only as an outbound
  fetch header), so a screenshot of the rendered page cannot contain it.
- `playwright-report/` and `test-results/` are gitignored -- local/CI run
  artifacts are never committed.
