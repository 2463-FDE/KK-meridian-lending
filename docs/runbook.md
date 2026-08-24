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
- **Ask what the note rate is:** `GET /los/pricing` — returns the configured
  rate, its source, and `is_production_pricing_policy: false`.
- **Generate an offer/disclosure:** `POST /los/offer {app_id, principal, term_months}`
  — **do not send `annual_rate_pct`.** The server sets the rate. A value that
  disagrees with the configured one is refused with a 422 rather than ignored, so
  an operator copying an older command against a non-default
  `DEMO_NOTE_RATE_PCT` gets a refusal, not a mispriced loan. Sending the
  server's own figure is accepted, for callers that still do.
  (origination → `disclosure-service`), or `/disclosure/*` directly.
- **Board an approved app to servicing:** `POST /los/applications/{id}/accept`.
- **Take a payment:** `/payments/*` → `payment-service` (captures the charge, then calls
  servicing `POST /accounts/{loan_id}/apply-payment` to post it). The legacy
  `POST /lss/payments` path is **gone** — deleted rather than disabled, so there
  is nothing to call and nothing to re-enable (`docs/DEBT.md` D2, asserted by
  `servicing-service/tests/test_legacy_payments_route_is_retired.py`). This line
  read "dead-but-present" until 2026-08-24, which is the phrasing D2 exists to
  argue against: a present-but-dead money route is one deployment away from
  live.
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
  writer -- servicing-service's legacy `POST /payments`, now retired (D2) -- which called no
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

## The payment review queue (D22)

**A different thing from a reconciliation break, and the screen keeps them
apart on purpose.** A break says the ledger and the settlement file disagree
about money. A review candidate says one payment resembles another and a person
should look. Reading a candidate as a break is the mistake this section exists
to prevent.

Open `/reconciliation` as csr, underwriter or admin. Two sections: **Payment
review candidates** at the top, **Reconciliation breaks** below.

**What puts an item there** (client decision, 2026-08-24 — this repository did
not choose the thresholds):

| Signal | What it means | Window |
|---|---|---|
| `exact_provider_transaction_id` | The processor returned a settlement reference another capture already holds | none — elapsed time is irrelevant |
| `exact_idempotency_key` | The same idempotency key was presented again | none |
| `heuristic_30_minute_candidate` | Same loan **and** amount **and** payment source **and** channel | rolling 30 minutes, inclusive |

All four factors are required for the heuristic. Same loan and same amount alone
raise nothing, because that is what a legitimate second installment looks like.

**Answering one.** Three dispositions and no fourth: `confirmed_duplicate`,
`legitimate_distinct_payment`, `requires_further_review`. Your name and role are
recorded from the verified session, not from anything the browser sends, and the
answer is **write-once** — there is no edit, and a second attempt returns 409.
Write a note if what you found is not obvious from the two rows.

**What answering does NOT do.** Nothing. No balance moves, no ledger entry is
written, the payment is untouched, and no maker-checker proposal is raised —
including for `confirmed_duplicate`. If money has to come back, that is a
separate two-person decision: raise it in `/approvals` and have a different
approver resolve it. A flag is not permission to move money, and the queue is
built so it cannot become one.

**If the queue is empty and you expected an item.** The signals are raised at
capture time by payment-service and are deliberately unable to fail a payment —
a review insert that errors is swallowed and logged rather than rolling back a
capture. So check `docker compose logs payment-service` for a swallowed insert
before concluding nothing matched.

## Following one payment across services

A borrower says they were charged and their balance did not move. Before
`db/migrations/0043` the only way to answer that was to join by eye --
`loan_id` plus amount plus a nearby timestamp -- across two services' logs and
two tables. That is what "payments feel flaky" looked like from the inside.

Every payment now carries a `correlation_id`, minted by payment-service at the
moment the charge is accepted and carried unchanged to the processor, to
servicing, and onto every ledger entry the payment writes.

**It correlates and nothing else.** No balance, no dedupe and no reconciliation
decision depends on it, so it is safe to quote in a ticket, and changing one
would move no money. It is NOT the idempotency key: that value is
caller-supplied and decides whether two requests are the same payment.

### Start from whatever the ticket gives you

```bash
# From a log line -- the id is on every payment-specific line in both services.
docker compose logs payment-service | grep pay_2f6c1e...
docker compose logs servicing-service | grep pay_2f6c1e...

# From a loan and an amount, when nobody has an id yet.
psql "$DATABASE_URL" -c "
  SELECT id, correlation_id, amount, auth_status, captured_at, applied_at
    FROM payments
   WHERE loan_id = 4471 AND amount = 250.00
   ORDER BY created_at DESC;"
```

### Then pull everything that belongs to it

```bash
psql "$DATABASE_URL" -c "
  SELECT id, loan_id, amount, auth_status, captured_at, applied_at, processor_ref
    FROM payments
   WHERE correlation_id = 'pay_2f6c1e...';"

# One row per component the payment moved: fees, interest, principal.
psql "$DATABASE_URL" -c "
  SELECT component, amount, entry_type, occurred_at
    FROM ledger_entries
   WHERE correlation_id = 'pay_2f6c1e...'
   ORDER BY occurred_at;"
```

Both columns are indexed (partial, on non-NULL), so neither query scans the
table.

### Reading the answer

| What you see | What happened |
|---|---|
| A `payments` row, no `ledger_entries` rows | Captured, never applied. The drain retries it -- check `apply_attempts` and `apply_last_error` |
| `payments` row and ledger rows | Applied. The ledger rows are where the money went, in the order fees -> interest -> principal |
| Two `payments` rows, one id | Impossible by construction: the id is per payment. Two ids for one complaint means two payments. **Check `/reconciliation` before doing anything else** -- since the client's decision of 2026-08-24 a pair like this is flagged for review automatically when it matches on loan, amount, payment source and channel inside 30 minutes, or on a repeated provider reference or idempotency key at any interval, and the item may already be there with a disposition on it (`docs/DEBT.md` D22). A flag is not a finding: it says a human should look, and only a human's recorded disposition says what it was |
| No rows at all | The charge never reached us. Look at the gateway, not here |

### The two cases with no id, and they are not faults

- **A payment captured before 0043** has `correlation_id` NULL and is not
  back-filled on retry. Its capture and authorization happened without an id, so
  a trace covering only the tail would look complete while being partial. Fall
  back to the `loan_id` + amount + timestamp join for those.
- **A ledger entry with no payment behind it** -- a late fee, an approved
  adjustment, a waiver -- carries NULL too. Those are found by `loan_id` and
  `entry_type`, and they are movements nobody paid for.

### What this is not

It follows a payment through **our** logs and tables. It is not a log
aggregator, and the processor is a mock in this repository -- a real processor
would have to be asked to echo `X-Correlation-Id` back on its own records
before the trace covered their side too.

## Verifying maker-checker on a running system

`scripts/check_self_approval.sh` answers a question CI cannot: is the
self-approval control live in the environment running **right now** -- after a
deploy, a config change, a database restore, or in front of somebody who wants
to see it rather than read about it. A passing CI badge and a deployed system
are different claims.

```bash
bash scripts/check_self_approval.sh          # exit code is the contract
#   0  verified       -- refused at every layer, and a second approver works
#   1  FAILED         -- a layer did not refuse. A control finding, not a flake
#   2  could not run  -- stack down, cannot log in, threshold unreadable
```

Exit 1 and exit 2 are deliberately distinct: *"the control is broken"* and
*"I could not tell"* call for different responses, and collapsing them is how a
control that never ran gets read as a control that passed -- the same defect
`reconciliation.peek` had before D7.

It removes one layer per step, so "the button was just disabled" is not an
available explanation:

| Step | What it removes | Refused by |
|---|---|---|
| 2 | the browser | the API, as the person who raised it |
| 3 | the API | `resolve_pending_movement()` |
| 4 | the function | `CHECK no_self_approval`, in the schema |
| 5 | *nothing* | **must SUCCEED** for a different approver |

Step 5 is what makes the rest mean anything. A system that refused *everyone*
would pass steps 2-4 exactly as a working one does, so a check that only ever
confirms refusal cannot distinguish "the control works" from "nothing works".

The admin threshold is read from the running `servicing-service`, never written
into the script: a second copy of a configured money value is free to drift from
the deployed one.

**Every self-resolution probe asks for `rejected`, never `approved`.** The guard
is on *who* resolves (`resolved_by <> requested_by`), not on which resolution is
asked for, so a self-rejection is refused by the same rule and tests the same
thing -- while an approval that slipped through would write a ledger entry and
move money. The only environment where one could slip through is the broken one
this script exists to find, so the check must be harmless *by construction*
rather than by trusting the control it is measuring:
`0037_resolve_pending_movement.sql` returns NULL on `rejected` **before**
reaching its `INSERT INTO ledger_entries`.

Each probe also gets its **own** proposal. A layer that breached and resolved a
shared row would make every later layer fail with "already resolved" -- an
invented finding masking the real one.

**What it leaves behind.** Four proposals, all resolved (rejected), none
approved. `pending_movements` refuses deletes by design -- a proposal is the
evidence of what staff asked for -- so the rows stay, and each run adds four.
**No money moves,** and that holds even if every control in the system is
broken. Step 6 prints `ledger_entry_id` rather than asserting it.

**What it does not cover.** It proves one person cannot approve their own
proposal. It does not address two colleagues colluding, and it is not a defence
against someone holding the schema-owning database credentials -- see ADR 0011,
*Limitations*, for what the schema still bounds in that case.

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
- **Month-end close — FIXED, with one part still open.** This bullet used to say
  `reconciliation.peek` totals do not tie out and nothing runs on a schedule.
  Both halves stopped being true: a `reconciliation` service runs the job on a
  schedule in the default compose services (not behind a profile), and the
  comparison is transaction-level, keyed on the processor's own settlement
  reference, so a break names the capture responsible instead of a net figure
  per loan. `peek` still exists and still reports two totals that need not tie
  out -- it is a read, not the control; the control is `app.reconcile_job` and
  its exit code. **What remains open is routing:** the alert rules in
  `monitoring/alerts.yml` reach `firing` in Prometheus and there is no
  Alertmanager in this compose file, so nothing pages anyone. Watch
  `docker compose logs -f reconciliation`, or the rules at
  `http://localhost:9090/alerts`, until somewhere to send a page has been
  decided (`docs/DEBT.md` D7).
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
