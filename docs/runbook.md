# Meridian Lending — Operations Runbook

> In-house ops notes. Sparse — Halcyon left no runbook, so this is what we've pieced
> together. Add to it when you learn something the hard way.

## Local / dev bring-up

```bash
cp .env.example .env     # NOTE: a populated .env is already committed, so this is optional
make up                  # docker compose up -d --build (postgres, redis, services, frontend)
make logs                # tail all services
make ps                  # container status
make down                # stop everything
```

- Portal: http://localhost:3000
- Gateway + OpenAPI docs: http://localhost:8000/docs
- Postgres: localhost:5432 (`meridian` / see `.env`)
- The DB auto-seeds from `db/init/*.sql` on first `up` (fresh volume only).

To re-apply the curated seed without recreating the volume:
```bash
make seed
```

To wipe and re-seed from scratch:
```bash
docker compose down -v && make up
```

## Demo logins

All seeded with password `password`:

| Username | Role | Use |
|----------|------|-----|
| `admin` | admin | full portal |
| `underwriter` | underwriter | decisioning views |
| `csr` | csr | servicing dashboard |
| `maria` | borrower | borrower view (applicant #1) |

## Health checks

```bash
curl localhost:8000/health     # gateway
curl localhost:8001/health     # origination (LOS, intake + boarding orchestrator)
curl localhost:8002/health     # servicing (LSS)
curl localhost:8003/health     # kyc-service
curl localhost:8004/health     # decision-service
curl localhost:8005/health     # disclosure-service
curl localhost:8006/health     # payment-service
```

Ports 8003–8006 are the four services extracted from the old origination monolith
(ADR 0004). `.env` carries their base URLs as `KYC_URL` / `DECISION_URL` /
`DISCLOSURE_URL` / `PAYMENT_URL` — origination reads these in `app/clients.py`.

## Common tasks

Endpoints are reached through the gateway. After the decomposition, decisioning,
disclosure, KYC, and payments are backed by their own services — origination still
orchestrates the LOS flow and calls them over HTTP.

- **Run a credit decision:** `POST /los/applications/{id}/decision` (origination orchestrates
  → `decision-service`), or hit `decision-service` directly via `/decision/*`.
- **Run a KYC/CIP check:** `/kyc/*` → `kyc-service` (origination also calls it inline during intake).
- **Generate an offer/disclosure:** `POST /los/offer {app_id, principal, annual_rate_pct, term_months}`
  (origination → `disclosure-service`), or `/disclosure/*` directly.
- **Board an approved app to servicing:** `POST /los/applications/{id}/accept`.
- **Take a payment:** `/payments/*` → `payment-service` (captures the charge, then calls
  servicing `POST /accounts/{loan_id}/apply-payment` to post it). The legacy `POST /lss/payments`
  path is dead-but-present.
- **Look at the portfolio:** `GET /lss/loans?limit=25&offset=0&status=current` (requires auth).
- **Reconciliation eyeball:** `GET /lss/reconciliation/peek` (ledger vs settlement totals).

## Reconciliation (D7)

Compares captured payments against the processor's settlement file, per loan, and
fails when they disagree.

```bash
# One run, from the servicing container. Exit code is the contract.
docker compose exec servicing-service python -m app.reconcile_job
#   0  clean          -- ran, everything within threshold
#   1  breach         -- ran, breaks exceeded the threshold   (a money finding)
#   2  could not run  -- settlement file missing, database down (a control finding)
```

**Schedule it as a separate process, not inside the API.** An in-process scheduler
dies with its web worker and nothing reports that it stopped, which is the failure
D7 already had: a control that silently is not running looks exactly like one that
is running and finding nothing.

```cron
# Daily, after the processor's settlement file lands. Cron mails on any non-zero
# exit, so a breach and a broken control both get noticed.
30 6 * * *  docker compose -f /srv/meridian/docker-compose.yml exec -T servicing-service python -m app.reconcile_job
```

Tune with `RECONCILIATION_BREAK_THRESHOLD` (default `0`). An unparseable or
negative value falls back to `0` rather than to permissive.

**What it reports.** Every run writes a `reconciliation_runs` row -- counts, signed
per-loan totals, the threshold it was judged against, and on failure the exception
TYPE only. `GET /reconciliation/peek` returns the two totals plus
`last_successful_run` and `recent_failures`, so "when did this last agree?" has an
answer. Prometheus gauges on the existing `/metrics`:

- `servicing_reconciliation_breaks`
- `servicing_reconciliation_break_value`
- `servicing_reconciliation_last_run_ok`
- `servicing_reconciliation_last_success_timestamp` -- the one to alert on. A run
  that stops happening produces no failures at all, so staleness is the signal.

**What it does NOT do, and do not plan around otherwise.**

- **No alerting integration.** There is no pager, no incident tool and no
  alertmanager in this build. What exists is a contract those things consume: the
  exit code, the table, the gauges. Calling it alerting would be the overclaim this
  work exists to remove.
- **No per-transaction matching.** The settlement file identifies a capture by the
  processor's `processor_ref`; `payments.authorization_id` is a different
  identifier from our own authorization call, and no payment row carries a `PR-`
  reference. So a break is detected but **not attributed** -- "loan 4471 disagrees
  by 99.99", not which capture caused it. Fixing that means persisting
  `processor_ref` at capture time in payment-service, and it is the next piece of
  work here.

## Known operational pain (unresolved)

- **Payment retries — FIXED, keep watching.** The processor occasionally times out and
  clients retry. This used to insert a second `payments` row and apply the charge twice.
  `payment-service` now requires an `idempotency_key` (`db/migrations/0007`) and a partial
  unique index enforces it, with apply-once protection on the servicing side (`payment_applications`). If a
  "double charge" ticket still arrives, it is a new bug, not this one: check whether the
  caller sent a *different* key for the same retry, which defeats the control by design.
- **Decision/disclosure/KYC stalls block applicants.** Origination calls these over
  synchronous HTTP with no timeout or retry. If `decision-service`'s credit pull hangs, the
  applicant-facing origination request hangs with it. Watch `decision-service` latency when
  intake requests pile up. (No circuit breaker / fallback.)
- **Month-end close.** `reconciliation.peek` totals do not tie out and nothing runs on a
  schedule. Finance reconciles by hand in a spreadsheet.
- **No log line writes a card-number-shaped value — and that is narrower than "no SSN".**
  This entry previously said `payment-service` logs full PAN/CVV/SSN at INFO and that
  origination logs full PII at intake. Both are false against the current code and were
  verified line by line. The guarantee is stated as card-number-shaped data rather than
  "no SSN" because one gap is unclosable by validation: servicing's legacy charge logs
  `loan_id` before the insert that would reject a nonexistent loan, and a nine-digit
  integer is both a plausible loan id and an SSN — `412559981` is accepted by any bound
  that does not also refuse real ids (see `servicing-service/app/logging_config.py`).
  Treat an SSN-shaped identifier in that log as possible; everything below is what was
  actually verified:
  `payment-service`'s `PaymentIn` sets `extra="forbid"` and accepts only a processor
  token plus `last4`/`brand`, so a field *named* `pan`/`cvv`/`ssn` is rejected with a
  422 (ADR 0008) — that is a check on field names, not on content, so it is the
  redactor and not the schema that covers card data pushed through an allowed field
  (a PAN in `processor_token`). `charge()` logs `redact_dict` output, which masks
  sensitive keys and runs the PAN/SSN/CVV patterns over every other string value, with
  the cardholder name omitted entirely;
  `servicing-service`'s legacy charge logs `loan_id`/`amount`/`method` only; and
  `origination-service`'s intake logs `app_id`/`applicant_id` (PR #6 review, Gap C).
  **What logs still carry:** identifiers and financial decision data — applicant,
  application and loan ids, model scores, decisions, reason codes, balances, amounts,
  and `last4`/`brand`. Those are the correlation fields operations needs; they are not
  claimed to be free of privacy consequence, and `last4` and processor tokens remain
  security-relevant. **Scope:** verified at application level only. Reverse-proxy,
  container-runtime and deployment-platform logging were not available in this
  repository and were not tested, so confirm those before shipping logs to a
  third-party aggregator. See `docs/DEBT.md` D5a.
- **Secrets are in the repo.** `.env` is committed and the services' `config.py` hardcode
  fallbacks — including Experian/core-banking keys in `decision-service` and the processor
  key in `payment-service`. Rotate before any real go-live. (Long-standing TODO.)

## Tests

```bash
make test    # runs pytest in both backend services (non-blocking)
```

`test_apr.py` (disclosure-service) and `test_money.py` (servicing-service) used to
FAIL by design, encoding float-rounding defects (D12/D6). Both are fixed now — a
real Decimal migration, not a weakened test (see `docs/ROADMAP.md`, Week 1). CI
no longer runs any service `continue-on-error`; every service's tests are blocking.
