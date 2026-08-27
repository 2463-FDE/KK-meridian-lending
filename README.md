# Meridian Lending Platform

> **No card data is stored. Still NOT PCI-DSS compliant.**
> `payment-service` used to store the full PAN and CVV in plaintext
> (`payments.pan`/`payments.cvv`, unencrypted `TEXT`) and log both at INFO — CVV/SAD
> storage is an absolute PCI-DSS prohibition regardless of encryption. Capture is
> tokenized in the browser (`adr/0008`, supersedes `adr/0003`): the service receives a
> processor token plus `last4`/`brand` and never a raw PAN, CVV or SSN. **The columns are
> gone** — `db/migrations/0031` dropped them from existing databases and
> `db/init/001_schema.sql` no longer creates them, so neither a migrated nor a freshly
> initialised database has a `payments.pan` or a `payments.cvv` at all
> (`docs/DEBT.md` D5b/D13).
>
> **That closes the defect and is not a compliance position.** A PCI-DSS claim needs a
> QSA assessment, a real processor and a scoped cardholder-data environment. This build
> has a *mocked* processor and no assessment of any kind, so the honest status is: the
> specific violation is fixed, compliance is unevaluated. Credit decisions are audited
> (Week 3's append-only `decision_events`); the rest of the compliance banner below is
> the original vendor's unverified claim, not a verified status.

The Meridian Lending Co. loan origination + servicing platform. Originally delivered by
Halcyon Software Group (now dissolved) as **three** backend services — `gateway`,
`origination-service` (LOS), `servicing-service` (LSS); maintained in-house since 2024-Q4.

> **Local training/demo build.** Everything here runs against `docker compose up` with
> seeded fictional data — no production environment, no real applicants, no real bureau or
> card rails. Capability claims in this README and in `ARCHITECTURE.md` use the status
> labels defined in [ARCHITECTURE.md § Status legend](ARCHITECTURE.md#status-legend)
> (Implemented and tested / Local/training-only / Deferred / Not production-ready / Fixed
> in PR #6 / Closed by PR #8). Naming a regulation identifies the rule a control is
> modelled on, not a compliance status.

This is a brownfield monorepo: a **Loan Origination System (LOS)** and a **Loan Servicing
System (LSS)** bolted together behind a single API gateway, with a Next.js borrower +
servicing portal. (Lending Ops asked for an "AI underwriting assistant"; it now exists as
`loan-assistant` — a LangChain agent behind a staff-only gateway route, which reaches the
lending-policy corpus through one bounded read-only tool and refuses to summarise when that
tool returns no policy evidence. See [ARCHITECTURE.md](ARCHITECTURE.md).)

Since the handoff the in-house team has begun **extracting the LOS monolith into focused
services**, partly to match the platform's intended target architecture. The platform now
runs **eight** backend services: the original three, four extracted ones —
`kyc-service`, `decision-service`, `disclosure-service`, `payment-service` — and
`loan-assistant`, which was added rather than extracted. (`reconciliation` in
`docker-compose.yml` is the servicing image running a scheduled job, not a ninth service.)
Origination is now an intake + boarding **orchestrator** that calls the new KYC, decision,
and disclosure services over synchronous HTTP; the old in-process `apr.py` / `fees.py` /
`offer.py` / `decision.py` / `kyc.py` modules moved out with them. This modernization is
**partial** — the data layer and most of the money-handling debt moved with the code
rather than being fixed.

## Architecture

```
                          ┌──────────────────────┐
  Next.js portal  ───────►│   gateway (BFF)      │  :8000
  (apply + servicing)     │   session auth/roles │
                          └─────────┬────────────┘
                                    │  /auth · /los · /lss · /kyc
                                    │  /decision · /disclosure · /payments
        ┌───────────────────────────┼───────────────────────────┐
        ▼                                                       ▼
 origination-service                                    servicing-service
   :8001  (LOS)                                           :8002  (LSS)
   intake + boarding orchestrator                         balances / delinquency /
        │  (sync HTTP, app/clients.py)                    reconciliation / loan reads
        ├──────────────┬──────────────┐                          ▲
        ▼              ▼              ▼                           │ apply-payment
   kyc-service   decision-service  disclosure-service             │
     :8003          :8004             :8005                  payment-service
   CIP identity   credit pull +     TILA/Reg-Z offer            :8006
                  scorecard         APR + amortization       card/ACH charge ─┘
        │              │              │                           │
        └──────────────┴──────────────┴───────────┬───────────────┘
                                                   ▼
                      Postgres :5432 (shared)  +   Redis :6379 (sessions)
```

Seven of the eight share **one** Postgres database and the same `db/init` schema + seed —
the data layer is unchanged by the decomposition. `loan-assistant` is the exception and
holds no database connection at all: it reads application data from origination-service over
HTTP, which is why no applicant row is reachable from the agent's process. The LOS↔LSS **seam** is still thin and
undocumented — a loan "boards" from origination to servicing by a direct insert into the
servicing schema. After a charge is captured, `payment-service` calls servicing's
`apply-payment` to post it. See `docs/architecture.md`.

## Quick start

```bash
make bootstrap            # creates .env and generates INTERNAL_SERVICE_TOKEN.
                         # docker-compose.yml supplies no default for it or for
                         # ENVIRONMENT: a token committed here is not a secret,
                         # and defaulting ENVIRONMENT to development would skip
                         # the token-strength checks on the money-moving routes.
                         # The generated token is local only -- never commit it.
make up                  # docker compose up -d (postgres, redis, all services, frontend)
make logs                # tail everything
make seed                # load db/init seed data (loans, payments, decisions)
make down
```

Portal: http://localhost:3000  ·  Gateway: http://localhost:8000/docs

Demo logins (all seeded with password `password`): `admin`, `underwriter`, `csr`,
and a borrower login `maria`.

## Services

| Path | Service | Port | Notes |
|------|---------|------|-------|
| `frontend/` | Next.js 15 portal | 3000 | application wizard + servicing dashboard |
| `services/gateway/` | FastAPI BFF | 8000 | session auth/roles; routes to LOS/LSS + KYC/decision/disclosure/payments |
| `services/origination-service/` | FastAPI (LOS) | 8001 | intake + LOS→LSS boarding orchestrator; calls KYC/decision/disclosure over HTTP |
| `services/servicing-service/` | FastAPI (LSS) | 8002 | balances, schedule, delinquency, reconciliation, `apply-payment` |
| `services/kyc-service/` | FastAPI | 8003 | CIP identity check; persists `kyc_checks` |
| `services/decision-service/` | FastAPI | 8004 | async credit pull + AI scorecard; **compute-only — persists nothing** (origination writes `decisions` and `decision_events`) |
| `services/disclosure-service/` | FastAPI | 8005 | TILA/Reg-Z offer + APR + amortization |
| `services/payment-service/` | FastAPI | 8006 | card/ACH charge; posts to servicing via `apply-payment` |
| `db/` | Postgres init + seed | 5432 | schema, migrations, seed data (shared by all services) |

## Compliance

**Not PCI-DSS compliant, and no card data is stored.** Those are two separate statements
and this section has been wrong about the second one, so both are made explicit.

**What is stored:** the processor's opaque token is used transiently and never persisted;
the `payments` row keeps `last4` and `brand` for display, and nothing else about the
instrument. **What is not stored:** there is no `payments.pan` and no `payments.cvv`.
`db/migrations/0031` dropped both from existing databases and `db/init/001_schema.sql`
never creates them, so a migrated database and a fresh one agree
(`docs/DEBT.md` D5b/D13, both recorded Fixed). Storing CVV/SAD post-authorization is a
flat PCI-DSS violation independent of encryption; it predated the current engagement
(vendor debt, `adr/0003`) and it is closed.

**Why that is still not compliance.** A PCI-DSS position requires a QSA assessment, a
real acquirer or processor, and a defined cardholder-data environment with the scoping
that follows. This build has a *mocked* processor and no assessment of any kind. Removing
stored card data closes a specific, serious violation; it evaluates nothing else, and
nothing in this repository should be read as a compliance claim.

*This section previously said the columns were "still there ... waiting to be dropped".
`0031` dropped them on 2026-08-10 and the sentence outlived it — the same defect the
paragraph below describes, one release later. `db/tests/test_readme_schema_claims.py`
now checks the schema claims here against the real schema, so the next drift fails a test
instead of waiting for a reader to notice.*

This section previously said that `payment-service` logs them at INFO and persists the PAN
and CVV itself. Both claims are false against the current code, and were verified against
it: `PaymentIn` sets `extra="forbid"` and accepts only a processor token plus
`last4`/`brand` (ADR 0008), so a field *named* `pan`, `cvv` or `ssn` is rejected with a 422
rather than dropped silently; its INSERT writes `last4`/`brand` and never the card number;
and `charge()` builds its log line through `redact_dict`, which masks sensitive keys and
runs the PAN/SSN/CVV patterns over every other string value — so card data pushed through
an *allowed* field (a PAN in `processor_token`, say) is redacted before it is logged, which
the schema alone would not prevent.
Neither the schema nor the seed data is an exposure any more: the columns are dropped and
the seeds insert `last4`/`brand` only — see `docs/DEBT.md` D5a for the per-call-site
logging verification.

Treat any prior claim of PCI-DSS compliance for this codebase as false.

Credit decisions ARE audited: every `/decisions` call persists an append-only
`decision_events` row (inputs, model score/version, reason codes — Week 3) alongside the
legacy outcome-only `decisions` table. ECOA/Reg B adverse-action reasons come from the
scorer itself, not a fixed string (see `services/decision-service/app/decision.py`).
SOX-controls and ECOA/Reg B process claims beyond the decision audit trail above are
unverified — do not represent them as confirmed without a real compliance review.

Compliance contact: Dana (VP Lending Ops). For SOX/reconciliation questions: Sam
(Controller). For fair-lending/BSA: Priya (Compliance Officer).

## Planning and debt

- [`docs/ROADMAP.md`](docs/ROADMAP.md) — the ten-week plan the ADRs and code comments cite.
- [`docs/DEBT.md`](docs/DEBT.md) — the `D`/`RF` register. Every `(debt D7)`-style
  citation in the source resolves here; it was being cited for weeks before it
  was written down anywhere.

## Known follow-ups (from the Halcyon handoff note)

> "Platform is secure and compliant. A few TODOs left in servicing but nothing
> blocking. — Halcyon"
