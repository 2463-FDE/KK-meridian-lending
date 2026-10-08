# Meridian Lending — Architecture

> Brownfield system. The platform was originally delivered by Halcyon Software Group
> (dissolved) and has been extended in place since. This document describes the current
> system; the architecture as first reconstructed at handover is in
> [`docs/history/inherited-architecture.md`](docs/history/inherited-architecture.md).

## Status legend

Every capability claim in this document, in `README.md`, and in `docs/model_card.md`
carries one of these labels, or is stated plainly enough not to need one. Read an
unlabelled claim as a description of code that exists, not as evidence that it works in
production — there is no production environment.

| Label | Means |
|---|---|
| **Implemented and tested** | Code exists and is covered by tests that run in CI (`.github/workflows/ci.yml`). |
| **Fixed** | A defect recorded in `docs/DEBT.md` whose fix is covered by a test that fails without it. |
| **Local/training-only** | Runs only against `docker compose up` with seeded fictional data. Never run against real applicants, real bureau, real card rails, or real volume. |
| **Deferred** | Deliberately not built. A decision, not an oversight — usually recorded in an ADR, `docs/ROADMAP.md`, or `docs/DEBT.md`. |
| **Not production-ready** | Code exists and may pass tests, but a known defect, missing control, or missing operational requirement makes it unsafe to run for real. |

Scope limit: this repository runs locally with synthetic data and simulated integrations.
Nothing here is an assertion of regulatory compliance. Where a regulation is named (TILA/Reg Z,
ECOA, BSA/CIP, PCI-DSS) it identifies the rule a control is *modelled on* — no control in this
repository has been reviewed by counsel, audited, or certified.

## System shape

Meridian is a consumer **personal-installment-loan** platform: two domains (origination
and servicing) joined behind one BFF gateway, with a Next.js portal. The inherited three
services (gateway, LOS, LSS) have been partially decomposed: KYC, decisioning, disclosure and
payments run as standalone services (ADR 0004), alongside an AI loan assistant and a
Prometheus/Grafana metrics stack. There are **eight** backend services; origination is an
intake + boarding orchestrator that fans out to the other services over synchronous HTTP and
also traverses the shared schema knowledge-graph style (`kg.py`) for the auto-disclosure and
fair-lending reporting paths below.

```
 Borrower / Servicing Rep ─► Next.js portal (3000)
                                   │  Authorization: Bearer <session> (optional for /los only)
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

 prometheus (9090) + grafana (3001) scrape /metrics off all 8 backend services.
```

### Network boundary and the internal token

Only `gateway` (8000) and `frontend` (3000) publish a host port in
`docker-compose.yml`; every backend service is reachable only by the gateway and
same-network callers, so the gateway's staff-only and ownership checks cannot be skipped by
calling a service directly. Defense in depth on top of that boundary is a shared
`X-Internal-Token`, enforced by origination, decision, disclosure, payment, kyc and
loan-assistant, and by servicing on its money routes — see "Auth & roles" below.
`services/gateway/tests/test_decision_service_not_host_published.py` derives its list of
services from `services/` on disk, so a new service is covered the day its directory
appears.

`kyc-service` checks the token on `POST /kyc/check`, and the gateway relays `/kyc/*` for
staff only, stripping any inbound `X-Internal-Token` so a caller cannot supply its own.

`servicing-service` checks the token on every money-moving route — `adjust-balance`,
`waive-fee`, `late-fee` and `apply-payment` — so reaching it on the network is not enough to
move a balance. The legacy `/payments` duplicate is retired (D2). Read routes are
ownership-checked at the gateway.

**The token is not supplied by this repository.** `docker-compose.yml` requires
`INTERNAL_SERVICE_TOKEN` to be set explicitly (`${INTERNAL_SERVICE_TOKEN:?…}`), and every
service refuses to start on an empty or repository-known value outside a development
environment: the failure this token defends against — a port re-exposed, the network
boundary bypassed — is exactly the one where an attacker could read a committed default.
Comparison uses `secrets.compare_digest`, so a wrong token leaks no timing signal about how
much of it was right.

### Money movement: accounting correctness over availability

`payment-service` preflights `servicing-service`'s authenticated `/internal/auth-check`
immediately before every card authorization, and that preflight **fails closed**: a
timeout, DNS failure, TLS error, 5xx, or a 200 that is not the expected body all
refuse the charge.

**A 200 means "I can accept and persist an apply-payment", not "our tokens match."**
That distinction is the contract, and getting it wrong is a real charge with no credit. The
check performs a real **write** — the same statements `apply_payment_once` runs, against the
same tables: an `INSERT` into `payment_applications` and an `UPDATE` of `balances.balance`,
inside a transaction it then rolls back, through the same `db.transaction()` helper, so the
same role and transaction semantics. A probe of a separate table would not do: per-table grant
drift or a trigger failure on either money table would leave the probe healthy. Reads are not
enough either: a read-only replica, a revoked `INSERT` grant, a read-only transaction or a
full disk all let `SELECT`s pass while the apply's `INSERT`/`UPDATE` fails. **Reads prove
reachability; only a write proves what this endpoint claims.** It is rolled back, so it
leaves no data and no sequence pressure on the tables the money lives in. So **card capture
is unavailable whenever servicing is unavailable** — a deliberate coupling, not an oversight.

Failing open would trade an outage for a worse outcome: the reconciler can drain
captured-but-unapplied rows only *once servicing returns*, and until then real money has left
a real card while the balance has not moved. An uncharged customer retries in a minute; a
charged customer with no credit files a complaint.

Two things bound the availability cost: the preflight timeout is short, so an outage
fails fast rather than hanging the request, and replaying an already-captured payment
never reaches the check, because it authorizes nothing.

**Who may authorize a movement.** `DEBT.md` **D8** is about who may *authorize* a money
movement, which is a different question from who can *reach* the endpoint — the token
answers the second one. D8 is closed, in four parts: the gateway's role rule
(`gateway/app/auth.py::can_move_money`, csr/admin only); a verified human principal
that servicing checks against a public key it cannot forge
(`servicing-service/app/principal.py`); a second approver — `adjust-balance`
and `waive-fee` raise proposals and move nothing, and `resolve_pending_movement`
refuses self-approval including admin (`db/migrations/0036`/`0037`); and a
ledger entry for every movement, naming the approver on an approved proposal. See
`docs/DEBT.md` D8 for the bounds, and `adr/0011-maker-checker-for-servicing-adjustments.md`
*Limitations* for the direct-`INSERT` boundary no application control changes.

## Services

| Service | Port | Tech | Owns / Responsibility |
|---------|------|------|-----------------------|
| `gateway` | 8000 | FastAPI + httpx + Redis | Session auth (`/auth/*`), role/ownership enforcement, reverse-proxy. Per-client-IP rate limiting (fixed window, fails open on a Redis outage). Forwards the resolved identity as `X-User-Id`/`X-User-Role`, stripping any inbound `X-User-*` the caller sent itself, and signs an Ed25519 principal assertion for servicing. See "Auth & roles" for the per-route tiers. |
| `origination-service` (LOS) | 8001 | FastAPI + SQLAlchemy + psycopg2 + LangGraph | Application intake & listing (intake logs `app_id`/`applicant_id` only, never the request payload — enforced by `tests/test_intake_pii_not_logged.py`; see `DEBT.md` D5c), LOS→LSS boarding seam (`intake.board_to_servicing`), and **orchestration** — calls kyc/decision/disclosure over synchronous HTTP via `app/clients.py`. `kg.py` walks the applicant→application→decision→offer chain (FK-linked relational data, no separate graph store) to drive `disclosure_graph.py`'s two-agent auto-offer-on-approval LangGraph and the reason-distribution report at `GET /applications/fair-lending/reason-distribution`. No runtime path groups decisions by ZIP or any other protected-class proxy (client decision, 2026-08-24). |
| `servicing-service` (LSS) | 8002 | FastAPI + SQLAlchemy + psycopg2 | Loan portfolio, balances, amortization schedule, delinquency/late fees, reconciliation and its review queue, maker-checker proposals, loan reads. `POST /accounts/{loan_id}/apply-payment` (called by payment-service) applies a captured charge by writing an immutable `ledger_entries` row; `balances` is the projection that trigger maintains (ADR 0010, `db/migrations/0035`). No host port; every money-moving route requires `X-Internal-Token` — `adjust-balance`, `waive-fee`, `late-fee` and `apply-payment` — and a gateway-signed principal. Read routes stay ownership-checked at the gateway. |
| `kyc-service` | 8003 | FastAPI + SQLAlchemy + psycopg2 | CIP-only identity check; persists `kyc_checks`, scoped to the application it ran for. No OFAC/sanctions, no UBO, no ongoing monitoring, no SAR (`DEBT.md` D11). No host port; `POST /kyc/check` requires `X-Internal-Token`. |
| `decision-service` | 8004 | FastAPI + LangGraph + psycopg2 | Credit pull + AI scorer chain (`decision.py`). **Compute-only — persists nothing.** Origination is the sole writer of both `decisions` and the append-only `decision_events` audit row, written atomically after its own finality recheck. The bureau call goes through a `BureauClient` seam that forwards an idempotency key so a retry after an ambiguous timeout recovers the original pull. Only `application_id` is trusted from a caller — everything else the model actually scores is loaded server-side from the application's own record. No host port; requires `X-Internal-Token`. |
| `disclosure-service` | 8005 | FastAPI + SQLAlchemy + psycopg2 | TILA/Reg-Z offer + APR + amortization (Decimal internally, float at the API boundary). `POST /offers` atomically checks decision approval and inserts (`INSERT ... SELECT ... FROM decisions WHERE outcome='approve'`) and is non-mutating on conflict (`ON CONFLICT DO NOTHING` + read-back) — a retry can never rewrite an already-disclosed loan's terms, even across a fee-rule change. `fee_pct_used` is snapshotted per offer. No host port; requires `X-Internal-Token`. |
| `payment-service` | 8006 | FastAPI + SQLAlchemy + psycopg2 | Card/ACH charge. **Idempotent**: `idempotency_key` is required at the API boundary and backed by a partial unique index, so a retried POST cannot double-charge (`db/migrations/0007`, re-asserted by `0010`; tests in `payment-service/tests/test_charge_flow.py`), with atomic dedupe + apply-once reconciliation (`0012`/`0013`). Card capture is tokenized client-side (ADR 0008, supersedes ADR 0003): the service never receives a raw PAN/CVV/SSN, only a processor token plus last4/brand, and the token itself is never persisted. After inserting the `payments` row it calls servicing's `apply-payment`. No host port; requires `X-Internal-Token`. |
| `loan-assistant` | 8007 | FastAPI + LangChain + local RAG retriever; AWS Bedrock by default | Two capabilities behind guardrails (redaction, corpus hygiene, cost guard on input tokens, fail-closed on a missing/unreachable model): `POST /applications/{id}/summary` (officer-facing risk tier + flags from a LangChain `create_agent` over `ChatBedrockConverse` with one bounded read-only policy tool, **staff-only** at the gateway) and `POST /policy-chat` (lending-policy Q&A, **staff-only at the gateway** — an internal tool for lending, compliance and underwriting staff, per `docs/DEBT.md` RF-28). The summary agent runs only on Bedrock; `LLM_PROVIDER` selects the model backend. Optional LangSmith tracing, and the two capabilities differ. The summary path suppresses the framework's own tracing entirely and emits a trace built from a structural allow-list of categorical and provenance fields instead (`app/trace.py`), parented under a `gateway_entry` run the gateway mints after it authorises the caller. Policy chat emits **no** trace: its client is not wrapped for tracing, because that wrapper would record the question and the completion. No host port; requires `X-Internal-Token`. |
| `frontend` | 3000 | Next.js 15 (App Router) | Borrower application wizard, offer/disclosure screen, servicing dashboard + loan detail, staff views. |
| `prometheus` / `grafana` | 9090 / 3001 | Prometheus + Grafana | Scrapes `/metrics` (request count, latency histograms, in-progress requests) off all 8 backend services. LangSmith tracing is limited to loan-assistant's allow-listed summary trace: `decision-service` and `origination-service` run LangGraph, which auto-instruments through `langchain-core` whenever `LANGSMITH_TRACING` is set, and both suppress it. |

### Data access — a partial ORM migration

Read paths (loan/application listing, detail, schedule, payment history) use **SQLAlchemy
2.0** ORM models (`models.py` + `database.py`). The older money-moving write paths
(`intake.py`, decisioning, payments, `balance.py`) use **raw psycopg2** (`db.py`).
The inherited migration to the ORM was never finished — this seam is intentional and is where
most of the money-handling code lives. The service decomposition (ADR 0004) did not change
this: the code that moved into `disclosure-service` / `payment-service` (and
`decision-service`'s read of application inputs) carries the same raw-psycopg2 pattern (money
itself is `NUMERIC` — see Data model below), and every service talks to the one shared schema
directly.

### Service-to-service wiring — synchronous coupling

Origination calls `kyc-service`, `decision-service`, and `disclosure-service` over
**synchronous HTTP** (`app/clients.py`), and `payment-service` calls servicing's
`apply-payment` to post a captured charge. This keeps the inherited synchronous-chain debt,
now across a network hop: a downstream `decision-service` stall (a slow bureau or
scoring-model call; decision-service itself is async) blocks the **applicant-facing**
origination request that is waiting on the HTTP call, bounded by a 30-second client timeout,
with no retry contract. Every server-to-server call carries the shared `X-Internal-Token`
header (see Auth & roles).

## Auth & roles

`users` table holds staff + borrower logins (`admin`, `underwriter`, `csr`, `borrower`).
Login → unsalted-sha256 password check (inherited; `docs/DEBT.md` SEC-01) → opaque token in
Redis (`session:<token>`, 8h TTL, no refresh/rotation, no CSRF token). The gateway resolves the
session and forwards `X-User-Id`/`X-User-Role` downstream, stripping any inbound `X-User-*`
and `X-Internal-Token` header the caller sent itself first (matched case-insensitively), so a
caller can neither claim a role nor supply its own internal token.

The gateway enforces per-route tiers rather than a single authenticated-or-not gate:

- **Anonymous-allowed**: `/los/*` only (an applicant can apply and check status without
  an account).
- **Staff-only**: `/decision/*`, `/disclosure/*`, `/kyc/*` (ops/inspection path only — the
  real decision/offer/CIP flow is origination calling those services server-to-server,
  never through the gateway), `/assistant/policy-chat` (an internal tool for lending,
  compliance and underwriting staff — a client decision, RF-28),
  `/assistant/applications/*/summary` (returns risk tier +
  internal underwriting flags a borrower shouldn't see about their own application), and
  the portfolio-wide/money-moving parts of `/lss/*`/`/payments/*` (list the whole
  portfolio, balance adjustments, fee waivers, reconciliation).

  `/kyc/*` is staff-only because it forwards straight to a service whose only authentication
  is the `X-Internal-Token` the gateway itself attaches. The anonymous path to CIP is
  `POST /los/applications`, where origination derives the identity from the row it just wrote
  instead of trusting the caller's body.
- **Owner-or-staff**: a specific loan's detail/schedule/payment-history/balance, and
  charging a payment — staff for any loan, a borrower only for a loan their own
  `applicant_id` owns.
- **Fail-closed**: anything else under a role-gated prefix 404s rather than being
  silently proxied with no authz decision made for it.

Downstream services (kyc/decision/disclosure/payment) trust the forwarded `X-User-Role`
without re-checking it themselves; for those, the gateway is the only enforcement point for
role/ownership.

**servicing-service verifies the human itself.** It checks a gateway-minted, Ed25519-signed
principal assertion — `principal.require_money_principal` and `require_staff_principal` — on
every money route, the maker-checker queue and the reconciliation review queue. A caller that
reaches servicing directly with the shared internal token is refused there for having no
verified human behind it, so the money routes do not rest on the gateway hop alone.

decision-service, disclosure-service, and payment-service add one more layer specifically
against a *network*-level bypass (not a role bypass): each requires a shared
`X-Internal-Token` header on its write route, checked fail-closed (an unset config token can
never match). This is deliberately narrow — it doesn't replace the gateway's role/ownership
logic, it only protects against the case where the network boundary (no host port) is ever
accidentally reopened.

## Data model (Postgres)

`users`, `applicants`, `applications`, `kyc_checks`, `decisions`, `decision_events`,
`offers`, `loans`, `balances`, `ledger_entries`, `payments`, `audit_logs`. Authoritative DDL:
`db/init/001_schema.sql`. Seed: `db/init/002_seed.sql` (curated anchors) +
`db/init/003_seed_bulk.sql` (synthetic portfolio of ~300 applications / ~180 loans / ~600
payments). Migrations under `db/migrations/` are hand-tracked and lag the init DDL —
notably `0005_money_columns_to_numeric.sql`, `0008_offer_decision_link.sql` +
`0009_offers_decision_id_unique.sql`, and `0014_add_applicant_zip.sql`.

- **Money** columns are `NUMERIC` (D12); `employment_years` stays float since it is a duration,
  not money.
- **`balances`** is a projection of immutable `ledger_entries`, maintained by 0035's trigger
  rather than overwritten by its last writer (ADR 0010).
- **`decisions`** records the outcome only; **`decision_events`** is a separate
  **append-only** audit row per decision — inputs, model score/version, top features, reason
  codes — so a decision can be proven, not just asserted.
- **`offers.decision_id`** is FK'd to `decisions.app_id` with a **unique** constraint, making
  offer creation idempotent per decision; a caller-supplied `decision_id` for an unrelated
  application is not trusted.
- **`applicants.zip_code`** (`db/migrations/0014_add_applicant_zip.sql`) is a postal-address
  component. The client prohibits ZIP and ZIP3 as a protected-class proxy (2026-08-24), and no
  runtime path groups decisions by this field.
- **Card data**: the `payments` table has no `pan` or `cvv` column, on either a migrated or a
  freshly initialised database. `db/migrations/0029_payments_backfill_last4.sql` back-filled
  `last4`, and `db/migrations/0031_drop_payments_pan_cvv.sql` dropped both card columns behind
  a gate that refuses without a completed back-fill and an explicit operator acknowledgement;
  `db/init/001_schema.sql` does not create them (ADR 0008, `docs/DEBT.md` D5b/D13).
- **Idempotent payments** (D2, Fixed): `payments.idempotency_key` is required at the API
  boundary (`ChargeIn.idempotency_key`, `min_length=1`) and enforced by a partial unique index
  (`db/migrations/0007`), with servicing-side apply-once protection in `payment_applications`
  (`db/migrations/0013`, `servicing-service/app/balance.py`).

## The LOS↔LSS seam

A funded loan is "boarded" by a direct cross-schema `INSERT` from origination into the
servicing `loans` + `balances` tables (`origination-service/app/intake.py::board_to_servicing`).
No boarding API, event, or contract. ADR 0002 records the shared-database decision.

A second cross-service write is on the servicing side: after `payment-service` captures a
charge and inserts the `payments` row, it calls `servicing POST
/accounts/{loan_id}/apply-payment` to post the payment against the balance. Behind that
endpoint, `balances` is a projection of immutable `ledger_entries`, so concurrent applies
cannot lose an update (D3, `db/migrations/0035`); the payment waterfall applies fees ->
accrued interest -> principal (D14, `servicing-service/app/waterfall.py`); and maker-checker
gates the two staff adjustment routes (D8, `db/migrations/0036`/`0037`).

A third is on the disclosure side: on an approved decision, origination's
`disclosure_graph.py` (two-node LangGraph: KG-read, then assemble) calls disclosure-service
server-to-server to auto-generate the offer, rather than waiting on a manual `POST /offer`
from the LOS UI.

## CI / supply chain

`.github/workflows/ci.yml`: gitleaks secret scan, blocking per-service pytest with coverage,
database migration tests against PostgreSQL 16, a `docker compose build` smoke test (catches a
Dockerfile that only builds on a developer machine with local state), a Playwright end-to-end
run, and a non-blocking `pip-audit`/`npm audit` dependency scan — no SAST tool. The scan's
findings are triaged in `docs/DEBT.md` SEC-11. It stays non-blocking because no
machine-readable advisory allowlist exists yet for the job to consult, so gating would fail
the build on findings SEC-11 has already dispositioned.

## Deployment considerations

The repository models the application architecture and its local service boundaries; no
production deployment exists and none is claimed. A production deployment would add, at
minimum:

- HTTPS ingress terminated at a load balancer or reverse proxy in front of the portal and
  gateway
- Backend services kept on a private network with no public addresses, as the Compose network
  does today
- Managed PostgreSQL and Redis in place of the Compose containers
- A secrets manager for the internal service token, principal signing key and model
  credentials
- Workload identity or per-service credentials for service-to-service authentication, in
  place of one shared token
- Encrypted service-to-service traffic where the environment requires it
- Centralized logs, metrics and traces collected from every service

## Local development

`docker compose up -d` brings up Postgres (auto-seeds from `db/init`), Redis, all eight
backend services, the gateway, the frontend, and the Prometheus/Grafana stack. See
`docs/runbook.md`.
