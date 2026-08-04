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
  Used read-only, only to assert test invariants ("exactly one
  application/decision/offer/loan") -- never written to by these tests.
  No default is committed; the suite fails fast with a clear error if this
  is unset (see `fixtures.ts::dbClient`).
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
