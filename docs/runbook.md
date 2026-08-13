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

The gateway is the only backend port published to the host:

```bash
curl localhost:8000/health     # gateway
```

Every other backend service is on the compose network only, so reach its health
endpoint through the container rather than the host:

```bash
docker compose exec origination-service  python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8001/health').read())"
docker compose exec servicing-service    python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8002/health').read())"
docker compose exec kyc-service          python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8003/health').read())"
docker compose exec decision-service     python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8004/health').read())"
docker compose exec disclosure-service   python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8005/health').read())"
docker compose exec payment-service      python -c "import urllib.request;print(urllib.request.urlopen('http://localhost:8006/health').read())"
```

Or read them all at once from `docker compose ps` — each service's compose
healthcheck already probes exactly these URLs.

*This block previously listed `curl localhost:8001`–`8006` directly.* Those had
not worked since PR #6 un-published 8001, 8002 and 8004–8006; 8003 followed when
the `kyc-service` bypass was closed. An operator following the old version got
`Connection refused` on six of seven lines and had no way to tell that from a
service being genuinely down — which is the failure a runbook exists to prevent.

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

Compares captured payments against the processor's settlement file **transaction
by transaction**, keyed on the processor's own settlement reference, and fails when
they disagree.

```bash
# One run, from the servicing container. Exit code is the contract.
docker compose exec servicing-service python -m app.reconcile_job
#   0  clean          -- ran, everything within threshold
#   1  breach         -- ran, breaks exceeded the threshold   (a money finding)
#   2  could not run  -- settlement file missing, database down (a control finding)
```

### It is scheduled by this repository

`docker compose up` starts a **`reconciliation`** service that runs the job on a
schedule. It is in the default services list, not behind a profile:

```bash
docker compose up -d                 # the scheduler starts with everything else
docker compose logs -f reconciliation
```

This used to be a command plus a paragraph telling you to wire cron yourself. A
normal deployment therefore kept answering `/health` while reconciliation never
ran once -- which is the failure D7 names. A control an operator has to remember
to enable is the same defect with an extra step, so it is on by default.

| Setting | Default | Meaning |
|---|---|---|
| `RECONCILE_INTERVAL_SECONDS` | `86400` | Seconds between runs. Lower it in a demo to watch it work. |
| `restart: unless-stopped` | — | The job exits non-zero on a breach or a control failure; the scheduler must survive that. An exit code is a finding to read, not a reason to stop reconciling. |

The scheduler decides only **when**. Whether a run was good is decided in
`reconciliation.run_and_record`, recorded in `reconciliation_runs`, and published
through the metrics below -- so a bug in the scheduler cannot manufacture a
success. It stops promptly on `SIGTERM` rather than sleeping through a shutdown.

**It runs as a separate process, not inside the API.** An in-process scheduler
dies with its web worker and nothing reports that it stopped.

### A run that compared nothing is an error, not a success

`within_threshold` used to be `break_value <= threshold` and nothing else. An
empty settlement file, a file with no usable `settlement_date`, or one whose
loans match nothing on the ledger all produce zero breaks -- because nothing was
compared -- so the run recorded `ok`, stamped `last_successful_run`, and
published a fresh success timestamp. A broken feed became indistinguishable from
a clean reconciliation, and the staleness alarm below went quiet in exactly the
way that means "healthy".

Each of these now records `outcome='error'`, leaves the last-success timestamp
untouched, and exits `2`:

| `error_code` | Cause | Who fixes it |
|---|---|---|
| `EmptySettlementFile` | Zero rows read | Whoever owns the feed |
| `IncompleteSettlementWindow` | No usable `settlement_date`, so the period is unknown | Whoever owns the file format |
| `NothingCompared` | Rows present, but no loan on either side | Scope or identifier mismatch |

Tune with `RECONCILIATION_BREAK_THRESHOLD` (default `0`). An unparseable or
negative value falls back to `0` rather than to permissive.

**Both sides are scoped to the same window.** The period comes from the settlement
file's own `settlement_date` values -- a daily file yields one day, a back-filled
file yields the range it covers -- and the ledger side is filtered to the same
dates on `payments.created_at`. Without that, one day's settlement is compared
against every payment ever recorded and almost every loan looks like a break; a
control that flags nearly everything reports nothing, because nobody can read it.

The window and the file's identity (name, row count, sha256 prefix) are recorded
on the run, so a result can be attributed to a period and a file. "0 breaks" over
an unknown window from an unknown file is not evidence.

**The job fails closed when it cannot leave evidence.** If the run record cannot
be written -- at the start or at the end -- it exits non-zero and never reports
`ok`. A control whose output is not recorded is a log line, and "when did this
last agree?" must not be answerable by a run that left no trace.

**What it reports.** Every run writes a `reconciliation_runs` row -- counts (loans
compared, references compared, unreferenced captures), signed per-reference totals,
the threshold it was judged against, and on failure the exception TYPE only. Each
break names the transaction (`processor_ref`) and its direction (`settlement_only`,
`ledger_only`, `amount_mismatch`, `unreferenced_capture`), so it can be
investigated rather than re-derived. `GET /reconciliation/peek` returns the two totals plus
`last_successful_run` and `recent_failures`, so "when did this last agree?" has an
answer. Prometheus gauges on the existing `/metrics`:

- `servicing_reconciliation_breaks`
- `servicing_reconciliation_break_value`
- `servicing_reconciliation_last_run_ok`
- `servicing_reconciliation_last_success_timestamp` -- the one the rules below
  watch. A run that stops happening produces no failures at all, so staleness is
  the signal.

**Alert rules are wired.** `monitoring/alerts.yml` is loaded by the `prometheus`
service and evaluated every 30s; the rules are visible at
<http://localhost:9090/alerts>.

| Alert | Fires when |
|---|---|
| `ReconciliationStale` | No successful run for over 26h (the daily schedule plus jitter) |
| `ReconciliationMetricMissing` | The metric is absent entirely -- the service is down, or reconciliation has never succeeded. A separate rule because `ReconciliationStale` cannot fire on a series that does not exist, which would make the loudest failure the quietest |
| `ReconciliationBreach` | The most recent run was not clean, including a run that compared nothing |

**What it does NOT do, and do not plan around otherwise.**

- **Alert rules fire; nothing is routed.** The rules above are genuinely
  evaluated by Prometheus and reach `firing`. There is **no Alertmanager** in this
  compose file, so nothing pages anyone, emails anyone or opens a ticket -- a
  firing alert has to be looked at on the Prometheus UI. Describing that as
  "alerting" would be the overclaim this work exists to remove, so `alerts.yml`
  says the same thing in its own header.

  Wiring an Alertmanager is a deployment decision -- where pages go, who is on
  call, what the escalation path is -- and is not one this repository can make.
- **Only processor-backed captures are compared.** `payments` has a second live
  writer -- servicing-service's legacy `POST /payments` -- which calls no
  processor, so no settlement file can contain a line for it. Those rows are
  labelled `capture_source = 'servicing_legacy'` (migration `0042`), excluded
  from the comparison and **counted** on the run as `out_of_scope_captures`, so
  the narrowing is visible. Rows written before `0042` whose provenance cannot be
  established are `'unknown'` and treated the same way. That the legacy route
  moves a balance with no processor behind it at all is D2, and this control does
  not close it.
- **A settlement row whose type is not `capture` or `refund`, whose amount is not
  a positive number, or whose amount carries sub-cent precision, fails the run**
  (`MalformedSettlementRows`). The sign comes from the type; a parser that
  guessed at a row it could not read would turn a feed-integrity problem into a
  money finding. And every amount is compared against `payments.amount`, which is
  `NUMERIC(14,2)` -- a row of `10.004` would round into a clean match against a
  `10.00` capture and report agreement on money this system cannot hold.
- **Captures written before migration 0041 cannot be matched.** `processor_ref` is
  persisted on every capture from that migration onward, but there was nothing to
  back-fill historical rows FROM -- `authorization_id` is minted by our own
  authorization call and appears in no settlement file. Such a row is reported as
  an `unreferenced_capture` break: money we recorded that no settlement line can
  corroborate. It is deliberately **not** skipped, because skipping it would
  understate our own side of the comparison. Expect these on the days legacy
  captures fall in; they are finite and self-clearing as the window moves.
- **A settlement file with no `processor_ref` column cannot be reconciled at all.**
  The run records `UnreferencedSettlementRows` and exits non-zero rather than
  falling back to comparing per-loan totals. That fallback is the defect this
  control was fixed out of -- two wrong transactions on one loan cancel and the run
  reports `ok` -- and reintroducing it silently, on a file already known to be
  malformed, is the worst moment to do it.

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
- **Provider keys have no rotation procedure.** `.env` is untracked and the hardcoded
  fallbacks are gone from all seven services, so this bullet no longer describes secrets
  sitting in the repository — it used to say `.env` is committed, which stopped being true
  and stayed on the page. The bureau, core-banking and processor keys are still supplied
  by environment alone with no rotation runbook and no expiry, which is the part that
  remains open. See `docs/ROADMAP.md` for why the historical values were placeholders
  rather than live credentials.

## Deployment order: kyc-service first

**kyc-service, then origination-service, then the frontend.** Not alphabetical, not
all at once.

Origination sends kyc-service only `application_id` and `applicant_id` -- the CIP
verdict is computed from the applicant row kyc-service reads for itself, so the
identity fields are redundant and sending them would put a second copy of the
applicant's SSN on the wire (review round 9).

An **old** kyc-service still requires `name`, `dob`, `ssn` and `address` as
strings and answers **422** to the identity-free payload. So deploying origination
first breaks every intake until kyc-service catches up.

It breaks *safely*: intake verifies a `kyc_checks` row landed for the application
and, when none did, returns the resumable 503 with the app id, access token and
resume token. The borrower retries with the same idempotency key and recovers the
same application -- no duplicate applicant, no application stuck at "submitted"
that nobody can advance. But every intake fails for the length of the window,
which is why the order matters rather than merely being tidy.

**Do not "fix" this by sending the identity fields again.** That undoes a
deliberate change and puts an SSN back on a wire that does not need it. The order
is the fix.

New kyc-service accepts **both** payload shapes -- its identity fields are
optional and ignored -- so old origination talking to new kyc-service works
throughout. Only the reverse combination is degraded, and only until the second
deploy lands.

## Rotating `INTERNAL_SERVICE_TOKEN`

The shared secret every service checks on internal calls. Read this before rotating,
because the current design **cannot** rotate without a brief outage window and pretending
otherwise is how a rotation turns into an incident.

**Why there is a window.** Each service compares the caller's `X-Internal-Token` against
its own single configured value with `secrets.compare_digest`. There is no
accept-old-and-new period. So between the first restarted service and the last, a caller
holding one value talks to a service expecting the other and gets a 401. Money-moving
routes fail closed during that gap, which is the correct direction and still an outage.

**Procedure.**

1. Generate: `python -c "import secrets; print(secrets.token_urlsafe(32))"`. At least 32
   characters and no placeholder words, or startup validation refuses it outside a
   development `ENVIRONMENT`.
2. Put it in the deployment's secret store and in your local gitignored `.env`. Never in
   this repository — a value committed here is not a secret, which is exactly the failure
   the token defends against.
3. Restart **all** services together rather than rolling. Rolling makes the window longer,
   not shorter, because every pair that disagrees fails for the whole roll.
4. Quiesce first if you can pick the moment: no in-flight card authorization means no
   payment can be captured against a servicing call that 401s mid-flight.
5. Verify after: `POST /kyc/check` with the OLD token returns 401, with the new one 200;
   an intake through `/los/applications` completes; a servicing money route 401s without a
   token. All three, because each covers a different service's copy of the value.

**If a zero-downtime rotation is ever required**, the change is to accept a list of valid
tokens rather than one — old and new both valid for the length of the rollout, then the
old one dropped. That is a code change to every service's `config.py` and its comparison,
not a runbook step, and it is not implemented today. Stated so nobody plans a
zero-downtime rotation against a system that cannot do one.

**If the token is believed compromised**, rotate immediately and accept the window: the
alternative is leaving a value that authorises money movement in an attacker's hands for
the length of a code change.

## Tests

```bash
make test    # runs pytest in both backend services (non-blocking)
```

`test_apr.py` (disclosure-service) and `test_money.py` (servicing-service) used to
FAIL by design, encoding float-rounding defects (D12/D6). Both are fixed now — a
real Decimal migration, not a weakened test (see `docs/ROADMAP.md`, Week 1). CI
no longer runs any service `continue-on-error`; every service's tests are blocking.
