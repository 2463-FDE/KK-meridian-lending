# Meridian Lending — Architecture

> Maintained by the in-house team. The platform was originally delivered by Halcyon
> Software Group (dissolved) and has been extended in-place since. Treat this as the
> current best understanding, not a clean-room design.

## Status legend

Every capability claim in this document, in `README.md`, and in `docs/model_card.md`
carries one of these labels, or is stated plainly enough not to need one. Read an
unlabelled claim as a description of code that exists, not as evidence that it works in
production — there is no production environment.

| Label | Means |
|---|---|
| **Implemented and tested** | Code exists and is covered by tests that run in CI (`.github/workflows/ci.yml`). |
| **Local/training-only** | Runs only against `docker compose up` with seeded fictional data. Never run against real applicants, real bureau, real card rails, or real volume. |
| **Deferred** | Deliberately not built. A decision, not an oversight — usually recorded in an ADR, `docs/ROADMAP.md`, or `docs/DEBT.md`. |
| **Not production-ready** | Code exists and may pass tests, but a known defect, missing control, or missing operational requirement makes it unsafe to run for real. |
| **Fixed in PR #6** | Was a defect; the fix is on `kalab-week4-disclosure-automation` with a test that fails without it. |
| **Closed by PR #8** | A defect PR #6 deliberately did not close, and PR #8 did (merged 2026-08-05). The label read "Still open for PR #8" while that was true. |

Scope limit: this repo is a **local training/demo build**. Nothing here is an assertion of
regulatory compliance. Where a regulation is named (TILA/Reg Z, ECOA, BSA/CIP, PCI-DSS) it
identifies the rule a control is *modelled on* — no control in this repo has been reviewed
by counsel, audited, or certified, and several are explicitly non-compliant (see the PCI
banner in `README.md`).

## System shape

Meridian is a consumer **personal-installment-loan** platform: two domains (origination
and servicing) bolted together behind one BFF gateway, with a Next.js portal. The original
three services (gateway, LOS, LSS) have since been decomposed — the in-house team extracted
KYC, decisioning, disclosure, and payments into standalone services (ADR 0004), then added
an AI loan assistant (Week 1-2) and a Prometheus/Grafana metrics stack (W7). There are now
**eight** backend services; origination is an intake + boarding orchestrator that fans out
to the other services over synchronous HTTP and also does its own knowledge-graph-style
traversal of the shared schema (`kg.py`, Week 4) for the auto-disclosure and fair-lending
paths below.

```
 Borrower / Servicing Rep ─► Next.js portal (3000)
                                   │  Authorization: Bearer <session> (optional for /los, /assistant/policy-chat)
                                   ▼
                          gateway / BFF (8000)  ── Redis (sessions, per-IP rate limit)
     /auth · /los · /lss · /kyc · /decision · /disclosure · /payments · /assistant
                     ┌───────────┴──────────────────────────────────────────┐
                     ▼                                                       ▼
        origination-service (8001)                                servicing-service (8002)
        LOS: intake + LOS→LSS boarding                             LSS: loans, balances,
        orchestrator + KG traversal (kg.py)                        schedule, delinquency,
        + note-rate publication (GET /pricing)                      reconciliation, apply-payment
                     │                                                       ▲
        ┌────────────┼─────────────┬───────────────────┐                    │ POST apply-payment
        ▼            ▼             ▼                   ▼                   │
  kyc-service  decision-service  disclosure-service  loan-assistant   payment-service (8006)
    (8003)        (8004)           (8005)              (8007)        card/ACH charge ────┘
  CIP identity  credit pull +    TILA/Reg-Z offer    RAG + guardrailed
                AI scorer        APR + amortization  LLM (summary, policy-chat)
                     └────────────┴─────────────┬────────────────┘
                                                 ▼
                                          Postgres (5432, shared)

 prometheus (9090) + grafana (3001) scrape /metrics off all 8 backend services (W7).
```

Only `gateway` (8000) and `frontend` (3000) publish a host port in
`docker-compose.yml`; every backend service is reachable only by the gateway and
same-network callers (review finding: services used to be host-published alongside the
gateway's own authz, which made its staff-only/ownership checks skippable by hitting the
service directly). Defense in depth on top of that boundary is a shared `X-Internal-Token`,
enforced by origination, decision, disclosure, payment, kyc and loan-assistant — see "Auth &
roles" below.

`kyc-service` **now checks the token on `POST /kyc/check`**, and the gateway relays
`/kyc/*` for staff only, stripping any inbound `X-Internal-Token` so a caller cannot
supply its own. It was the one service that was *both* host-published *and* unauthenticated,
so `POST localhost:8003` wrote a CIP compliance record for any applicant id it named —
verified against a running stack, not inferred.

*This paragraph read "**Still open (not fixed in PR #6)**" for two months, and the delay is
the finding.* PR #8 shipped card tokenization instead, nothing reassigned the gap, and the
regression test that would have caught it named four services by hand and omitted this one.
**A partial enumeration in a security test reads exactly like a complete one.** Both halves
of the boundary are asserted now rather than described, and
`services/gateway/tests/test_decision_service_not_host_published.py` derives its list of
services from `services/` on disk — so a new service is covered the day its directory
appears, not the day someone remembers to add it.

`servicing-service` **now checks the token on every money-moving route** —
`adjust-balance`, `waive-fee`, `late-fee` and `apply-payment`. (A fifth, the legacy
`/payments` duplicate, was retired with D2 — it no longer exists to guard.) It is not host-published either, but that was the *only* control it had, and
"not published" is network topology rather than an application-level check: any container
that could resolve `servicing-service:8002` could set a balance to zero. Its read routes
are unchanged — they are ownership-checked at the gateway. *This paragraph previously read
"`servicing-service` doesn't check the token either … Both are tracked for PR #8"; PR #8
shipped card tokenization instead and closed neither.*

**The token itself is no longer supplied by this repository.** `docker-compose.yml`
required `INTERNAL_SERVICE_TOKEN` to be set explicitly (`${INTERNAL_SERVICE_TOKEN:?…}`)
and every service refuses to start on an empty or repository-known value outside a
development environment. A fallback committed here was not a secret: the failure this
token defends against — a port re-exposed, the network boundary bypassed — is precisely
the one where an attacker can read the default out of the repo, so the guard passed while
protecting nothing. Comparison uses `secrets.compare_digest`, so a wrong token leaks no
timing signal about how much of it was right.

**For money movement, accounting correctness beats availability.** Written down
because it is a real tradeoff that was decided the wrong way once already, and the
next person will face the same argument.

`payment-service` preflights `servicing-service`'s authenticated `/internal/auth-check`
immediately before every card authorization, and that preflight **fails closed**: a
timeout, DNS failure, TLS error, 5xx, or a 200 that is not the expected body all
refuse the charge.

**A 200 means "I can accept and persist an apply-payment", not "our tokens match."**
That distinction is the contract, and getting it wrong is a real charge with no credit:
an earlier version authenticated and returned without touching the database, so a
servicing process that was up with its database down answered 200, the card was
captured, and the follow-up `apply-payment` failed. The check performs a real **write** — the same
statements `apply_payment_once` runs, against the same tables: an `INSERT` into
`payment_applications` and an `UPDATE` of `balances.balance`, inside a transaction it then
rolls back, through the same `db.transaction()` helper, so the same role and transaction
semantics. It probed a dedicated `preflight_writes` table first, which was the same defect
one level down: per-table grant drift or a trigger failure on either money table left the
probe table healthy, so a proxy for the thing is not the thing. Reads were not enough: a read-only replica, a revoked `INSERT`
grant, a read-only transaction or a full disk all let `SELECT`s pass while the apply's
`INSERT`/`UPDATE` fails, and a 200 still greenlit an uncreditable capture. **Reads prove
reachability; only a write proves what this endpoint claims.** It is rolled back, so it
leaves no data and no sequence pressure on the tables the money lives in. So **card capture is unavailable whenever servicing is
unavailable** — a deliberate coupling, not an oversight.

The first version failed *open*, on the reasoning that "unknown is not known-bad" and
that refusing payments during every servicing blip trades a rare accounting error for
a common outage. That is wrong here on both counts. It left the guard catching only an
explicit 401 — the narrow case — while the broad case, servicing simply being down,
sailed past it. And the fallback argument, that the reconciler drains
captured-but-unapplied rows, only holds *once servicing returns*: until then real money
has left a real card while the balance has not moved. An uncharged customer retries in
a minute; a charged customer with no credit files a complaint.

Two things bound the availability cost: the preflight timeout is short, so an outage
fails fast rather than hanging the request, and replaying an already-captured payment
never reaches the check, because it authorizes nothing.

**What this does not close.** `DEBT.md` **D8** is about who may *authorize* a money
movement, which is a different question from who can *reach* the endpoint — the token
answers the second one, and closing either leaves the other open. **D8 itself is now
closed**, in four parts: the gateway's role rule
(`gateway/app/auth.py::can_move_money`, csr/admin only); a verified human principal
that servicing checks against a public key it cannot forge
(`servicing-service/app/principal.py`, PR #33); a second approver — `adjust-balance`
and `waive-fee` raise proposals and move nothing, and `resolve_pending_movement`
refuses self-approval including admin (`db/migrations/0036`/`0037`, PRs #34/#35); and a
ledger entry for every movement, naming the approver on an approved proposal. See
`docs/DEBT.md` D8 for the bounds, and `adr/0011-maker-checker-for-servicing-adjustments.md`
*Limitations* for the direct-`INSERT` boundary no application control changes.

*Historical, kept because the drift is instructive:* this passage first described all
of D8 as open long after two thirds of it had landed, and was then corrected to
"partly closed — servicing validates no human principal and no second approver
exists". That correction was itself out of date within a week: both halves landed in
PRs #33-#35, and the sentence outlived them. `db/tests/test_d8_is_closed_everywhere.py`
now fails on either wording.

## Services

| Service | Port | Tech | Owns / Responsibility |
|---------|------|------|-----------------------|
| `gateway` | 8000 | FastAPI + httpx + Redis | Session auth (`/auth/*`), role/ownership enforcement, reverse-proxy. Per-client-IP rate limiting (fixed window, fails open on a Redis outage). Forwards the resolved identity as `X-User-Id`/`X-User-Role`, stripping any inbound `X-User-*` the caller sent itself. See "Auth & roles" for the per-route tiers. |
| `origination-service` (LOS) | 8001 | FastAPI + SQLAlchemy + psycopg2 | Application intake & listing (intake logs `app_id`/`applicant_id` only, never the request payload — enforced by `tests/test_intake_pii_not_logged.py`; this cell previously claimed a request-logging middleware in `logging_config.py` logged full POST bodies, which is false — no such middleware has ever existed, see `DEBT.md` D5c), LOS→LSS boarding seam (`intake.board_to_servicing`), and **orchestration** — calls kyc/decision/disclosure over synchronous HTTP via `app/clients.py`. `kg.py` walks the applicant→application→decision→offer chain (FK-linked relational data, no separate graph store) to drive `disclosure_graph.py`'s two-agent auto-offer-on-approval LangGraph and the reason-distribution report at `GET /applications/fair-lending/reason-distribution`. It drove a ZIP3 fair-lending screen until 2026-08-24, when the client prohibited any protected-class proxy including one derived from ZIP; `fair_lending.py` was deleted rather than disabled (PR #78), and `kg.py` no longer references ZIP at all. |
| `servicing-service` (LSS) | 8002 | FastAPI + SQLAlchemy + psycopg2 | Loan portfolio, balances, amortization schedule, delinquency/late fees, reconciliation peek, loan reads. `POST /accounts/{loan_id}/apply-payment` (called by payment-service) applies a captured charge by writing an immutable `ledger_entries` row; `balances` is the projection that trigger maintains (ADR 0010, `db/migrations/0035`). This cell said "still a single mutable column, no ledger" — a wording variant of the claim corrected further down this same file, which is why the regression pins now match the concept rather than one phrasing. No host port; every money-moving route requires `X-Internal-Token` — `adjust-balance`, `waive-fee`, `late-fee` and `apply-payment`. (A fifth, the legacy `/payments` duplicate, was retired with D2; it is listed in the guarded set nowhere because it no longer exists.) Read routes stay ownership-checked at the gateway. |
| `kyc-service` | 8003 | FastAPI + SQLAlchemy + psycopg2 | CIP-only identity check; persists `kyc_checks`, scoped to the application it ran for. No OFAC/sanctions, no UBO, no ongoing monitoring, no SAR (`DEBT.md` D11). No host port; `POST /kyc/check` requires `X-Internal-Token` — this cell read "Host-published **and** does not check `X-Internal-Token`", which was the bypass described in the boundary note above. |
| `decision-service` | 8004 | FastAPI + LangGraph + psycopg2 | Credit pull + AI scorer chain (`decision.py`). **Compute-only — persists nothing.** Origination is the sole writer of both `decisions` and the append-only `decision_events` audit row, written atomically after its own finality recheck (PR #6). The bureau call goes through a `BureauClient` seam that forwards an idempotency key so a retry after an ambiguous timeout recovers the original pull. Only `application_id` is trusted from a caller — everything else the model actually scores is loaded server-side from the application's own record. No host port; requires `X-Internal-Token`. |
| `disclosure-service` | 8005 | FastAPI + SQLAlchemy + psycopg2 | TILA/Reg-Z offer + APR + amortization (Decimal internally, float at the API boundary). `POST /offers` atomically checks decision approval and inserts (`INSERT ... SELECT ... FROM decisions WHERE outcome='approve'`) and is non-mutating on conflict (`ON CONFLICT DO NOTHING` + read-back) — a retry can never rewrite an already-disclosed loan's terms, even across a fee-rule change. `fee_pct_used` is snapshotted per offer. No host port; requires `X-Internal-Token`. |
| `payment-service` | 8006 | FastAPI + SQLAlchemy + psycopg2 | Card/ACH charge. **Idempotent** since PR #6: `idempotency_key` is required at the API boundary and backed by a partial unique index, so a retried POST no longer double-charges (`db/migrations/0007`, re-asserted by `0010`; tests in `payment-service/tests/test_charge_flow.py`), with atomic dedupe + apply-once reconciliation (`0012`/`0013`). **PCI debt closed in PR #8:** card capture is tokenized client-side (ADR 0008, supersedes ADR 0003) — the service never receives a raw PAN/CVV/SSN, only a processor token plus last4/brand, and the token itself is never persisted. After inserting the `payments` row it calls servicing's `apply-payment`. No host port; requires `X-Internal-Token`. |
| `loan-assistant` | 8007 | FastAPI + LangGraph/RAG + Anthropic or Bedrock | Two capabilities behind guardrails (redaction, corpus hygiene, cost guard on input tokens, fail-closed on a missing/unreachable model): `POST /applications/{id}/summary` (officer-facing risk tier + flags, **staff-only** at the gateway) and `POST /policy-chat` (generic lending-policy Q&A, **open to any caller including anonymous**, same pattern as `/los/*`). Optional LangSmith tracing, and the two capabilities differ. The summary path suppresses the framework's own tracing entirely and emits a trace built from a structural allow-list of categorical and provenance fields instead (`app/trace.py`), parented under a `gateway_entry` run the gateway mints after it authorises the caller. Policy chat emits **no** trace: its client is not wrapped for tracing, because that wrapper recorded the question and the completion. The claim this row used to make -- that traces were "PII-scrubbed by the same guardrails" -- was measured and was false: those guardrails run on what the summary RETURNS, while the tracer serialised the whole run. No host port; requires `X-Internal-Token`. |
| `frontend` | 3000 | Next.js 15 (App Router) | Borrower application wizard, offer/disclosure screen, servicing dashboard + loan detail. |
| `prometheus` / `grafana` | 9090 / 3001 | Prometheus + Grafana | Scrapes `/metrics` (request count, latency histograms, in-progress requests) off all 8 backend services. No cross-service metrics existed before this (W7). The claim that LangSmith "only ever covered loan-assistant's own LLM calls" was wrong: `decision-service` and `origination-service` both run LangGraph, which auto-instruments through `langchain-core` whenever `LANGSMITH_TRACING` is set, and both were exporting their graph state. Both now suppress it. |

### Data access — a partial ORM migration

Read paths (loan/application listing, detail, schedule, payment history) use **SQLAlchemy
2.0** ORM models (`models.py` + `database.py`). The older money-moving write paths
(`intake.py`, decisioning, payments, `balance.py`) still use **raw psycopg2** (`db.py`).
The migration to the ORM was never finished — this seam is intentional and is where most
of the money-handling debt lives. The service decomposition (ADR 0004) did **not** clean
this up: the write-path code moved into `decision-service` / `disclosure-service` /
`payment-service` carrying the same raw-psycopg2 pattern (though money itself is no longer
float — see Data model below), and every service still talks to the one shared schema
directly.

### Service-to-service wiring — a new synchronous coupling

Origination no longer decides, discloses, or KYCs in-process. It now calls `kyc-service`,
`decision-service`, and `disclosure-service` over **synchronous HTTP** (`app/clients.py`),
and `payment-service` calls servicing's `apply-payment` to post a captured charge. This
re-creates the original synchronous-chain debt at a worse altitude: a downstream
`decision-service` stall (its credit pull blocks the thread) now blocks the
**applicant-facing** origination request that is waiting on the HTTP call — the same
"synchronous decisioning chain" flaw, now spanning a network hop with no timeout/retry
contract. Every server-to-server call into decision/disclosure/payment now also carries a
shared `X-Internal-Token` header (see Auth & roles).

## Auth & roles

`users` table holds staff + borrower logins (`admin`, `underwriter`, `csr`, `borrower`).
Login → unsalted-sha256 password check → opaque token in Redis (`session:<token>`, 8h
TTL, no refresh/rotation, no CSRF token). The gateway resolves the session and forwards
`X-User-Id`/`X-User-Role` downstream, stripping any inbound `X-User-*` the caller sent
itself first (a caller used to be able to spoof `X-User-Role: admin` on an otherwise-
anonymous route). `X-Internal-Token` is stripped from inbound requests for the same
reason and was not: header names arrive lowercased, so a client's `X-Internal-Token`
survived as a separate dict key from the one the gateway adds, both reached the wire, and
the downstream `Header(alias=...)` read the client's. Any caller could hand the gateway a
junk token and force a 401 on every internal-token route — fail-closed, so availability
rather than escalation, but caller-controlled.

The gateway enforces per-route tiers rather than a single authenticated-or-not gate:

- **Anonymous-allowed**: `/los/*` (an applicant can apply/check status without an
  account) and `/assistant/policy-chat` (generic policy Q&A, no per-applicant financials).
- **Staff-only**: `/decision/*`, `/disclosure/*`, `/kyc/*` (ops/inspection path only — the
  real decision/offer/CIP flow is origination calling those services server-to-server,
  never through the gateway), `/assistant/applications/*/summary` (returns risk tier +
  internal underwriting flags a borrower shouldn't see about their own application), and
  the portfolio-wide/money-moving parts of `/lss/*`/`/payments/*` (list the whole
  portfolio, balance adjustments, fee waivers, reconciliation).

  `/kyc/*` was **anonymous-allowed** until this was corrected, on the reasoning that an
  applicant applies without an account. That reasoning holds for `/los/*`, which gates its
  own sensitive routes — but `/kyc/*` forwards straight to a service whose only auth is the
  `X-Internal-Token` *the gateway itself attaches*, so an anonymous caller got the gateway
  to sign the request for it and could write a `kyc_checks` row for any `applicant_id`.
  The anonymous path to CIP is `POST /los/applications`, where origination derives the
  identity from the row it just wrote instead of trusting the caller's body.
- **Owner-or-staff**: a specific loan's detail/schedule/payment-history/balance, and
  charging a payment — staff for any loan, a borrower only for a loan their own
  `applicant_id` owns.
- **Fail-closed**: anything else under a role-gated prefix 404s rather than being
  silently proxied with no authz decision made for it.

Downstream services (kyc/decision/disclosure/payment) still trust the forwarded
`X-User-Role` without re-checking it themselves; for those, the gateway is the only
enforcement point for role/ownership.

**servicing-service is the exception, and the sentence above included it until
2026-08-24.** It verifies a gateway-minted, Ed25519-signed principal assertion
itself — `principal.require_money_principal` and `require_staff_principal`, on
every money route, the maker-checker queue and the reconciliation review queue
(PR #33 introduced it; PR #81 added the newest two routes). So a caller that
reaches servicing directly with the shared internal token is refused there for
having no verified human behind it, rather than arriving
authenticated-as-a-human. The old wording understated the control that exists
and would have let a reader conclude the money routes rest on one hop. decision-service, disclosure-service, and payment-service add one
more layer specifically against a *network*-level bypass (not a role bypass): each requires
a shared `X-Internal-Token` header on its write route, checked fail-closed (an unset
config token can never match). This is deliberately narrow — it doesn't replace the
gateway's role/ownership logic, it only protects against the case where the network
boundary (no host port) is ever accidentally reopened.

## Data model (Postgres)

`users`, `applicants`, `applications`, `kyc_checks`, `decisions`, `decision_events`,
`offers`, `loans`, `balances`, `payments`, `audit_logs`. Authoritative DDL:
`db/init/001_schema.sql`. Seed: `db/init/002_seed.sql` (curated anchors) +
`db/init/003_seed_bulk.sql` (synthetic portfolio of ~300 applications / ~180 loans / ~600
payments). Migrations under `db/migrations/` are hand-tracked and lag the init DDL —
notably `0005_money_columns_to_numeric.sql`, `0008_offer_decision_link.sql` +
`0009_offers_decision_id_unique.sql`, and `0014_add_applicant_zip.sql`.

Money columns are `NUMERIC` (D12 fix — every dollar-amount column used to be `DOUBLE
PRECISION`; `employment_years` stayed float since it's a duration, not money). `balances`
is a projection of `ledger_entries`, maintained by 0035's trigger rather than
overwritten by its last writer — this line read "is still a single mutable column (no
ledger)" after that shipped. `decisions` records the outcome only;
`decision_events` (Week 3) is a separate **append-only** audit row per decision — inputs,
model score/version, top features, reason codes — so a decision can be proven, not just
asserted. `offers.decision_id` is now FK'd to `decisions.app_id` with a **unique**
constraint, making offer creation idempotent per decision and closing a leaked-decision
path where a caller-supplied `decision_id` for an unrelated application used to be trusted
verbatim. `applicants.zip_code` (`db/migrations/0014_add_applicant_zip.sql`) is a postal-address
component. It was added in Week 8 to back a ZIP3-level four-fifths-rule screen; **the client
prohibited ZIP and ZIP3 as a protected-class proxy on 2026-08-24**, that screen and its route are
retired, and no runtime path groups decisions by this field. The column stays because an address
without a ZIP is an incomplete address. *This sentence said the field "backs the ZIP3-level
four-fifths-rule disparate-impact screen (`fair_lending.py`)" until that decision.* `payments.pan`/`cvv` are **gone, not dormant**: the writers went first (PR #8,
PR #11), `db/migrations/0029_payments_backfill_last4.sql` back-filled `last4`, and
`db/migrations/0031_drop_payments_pan_cvv.sql` dropped both columns behind a gate that refuses
without a completed back-fill and an explicit operator acknowledgement. `db/init/001_schema.sql`
no longer creates them either, so neither a migrated nor a freshly initialised database has a
`payments.pan` or a `payments.cvv` at all (ADR 0008, `docs/DEBT.md` D5b/D13). *This sentence
previously said the two survived as nullable legacy columns, which was true until 0031 landed.* The
retried-POST double-charge that D2 described is CLOSED: `payments.idempotency_key` is
required at the API boundary (`ChargeIn.idempotency_key`, `min_length=1`) and enforced by
a partial unique index (`db/migrations/0007`), with servicing-side apply-once protection in
`payment_applications` (`db/migrations/0013`, `servicing-service/app/balance.py`).

## The LOS↔LSS seam

A funded loan is "boarded" by a direct cross-schema `INSERT` from origination into the
servicing `loans` + `balances` tables (`origination-service/app/intake.py::board_to_servicing`).
No boarding API, event, or contract. ADR 0002.

A second cross-service write now exists on the servicing side: after `payment-service`
captures a charge and inserts the `payments` row, it calls `servicing POST
/accounts/{loan_id}/apply-payment` to post the payment against the balance. The balance-mutation debt that used to live behind that endpoint has since been
worked through, and none of the four items is still open: the lost update is gone
because `balances` is a projection of immutable `ledger_entries` (D3,
`db/migrations/0035`), the same change removed the mutable-total write, the
payment waterfall applies fees -> accrued interest -> principal (D14,
`servicing-service/app/waterfall.py`), and maker-checker gates the two staff
routes (D8, `db/migrations/0036`/`0037`). *This paragraph read "the
balance-mutation debt (race / lost-update, mutable balance, no payment waterfall,
no maker-checker) lives behind that endpoint and is unchanged" until those
landed.*

A third now exists on the disclosure side: on an approved decision, origination's
`disclosure_graph.py` (two-node LangGraph: KG-read, then assemble) calls disclosure-service
server-to-server to auto-generate the offer, rather than waiting on a manual `POST /offer`
from the LOS UI.

## CI / supply chain

`.github/workflows/ci.yml`: gitleaks secret scan, per-service pytest with coverage
(blocking — the `|| true` that used to mask every test failure, including a missing
pytest install, is gone), a `docker compose build` smoke test (catches a Dockerfile that
only builds on a dev machine with local state, like the CA-bundle-copy break that shipped
once), and a non-blocking `pip-audit`/`npm audit` dependency scan (first run, findings not
yet triaged — no SAST tool yet).

## Local development

`docker compose up -d` brings up Postgres (auto-seeds from `db/init`), Redis, all eight
backend services, the gateway, the frontend, and the Prometheus/Grafana stack. See
`docs/runbook.md`.
