# Meridian Lending Platform

> **Still NOT PCI-DSS compliant, but the capture path no longer stores card data.**
> `payment-service` used to store the full PAN and CVV in plaintext
> (`payments.pan`/`payments.cvv`, unencrypted `TEXT`) and log both at INFO — CVV/SAD
> storage is an absolute PCI-DSS prohibition regardless of encryption. PR #8 tokenizes
> capture in the browser (`adr/0008`, supersedes `adr/0003`): the service now receives a
> processor token plus last4/brand and never a raw PAN, CVV or SSN. **What remains:**
> `payments.pan`/`cvv` are still nullable columns holding whatever pre-tokenization rows
> already wrote, and nothing purges them yet. A compliance claim needs that purge, a
> QSA/SAQ assessment and a real processor — none of which exist here. Credit decisions
> are audited (Week 3's append-only `decision_events`); the rest of the compliance banner
> below is the original vendor's unverified claim, not a verified status.

The Meridian Lending Co. loan origination + servicing platform. Originally delivered by
Halcyon Software Group (now dissolved) as **three** backend services — `gateway`,
`origination-service` (LOS), `servicing-service` (LSS); maintained in-house since 2024-Q4.

> **Local training/demo build.** Everything here runs against `docker compose up` with
> seeded fictional data — no production environment, no real applicants, no real bureau or
> card rails. Capability claims in this README and in `ARCHITECTURE.md` use the status
> labels defined in [ARCHITECTURE.md § Status legend](ARCHITECTURE.md#status-legend)
> (Implemented and tested / Local/training-only / Deferred / Not production-ready / Fixed
> in PR #6 / Still open for PR #8). Naming a regulation identifies the rule a control is
> modelled on, not a compliance status.

This is a brownfield monorepo: a **Loan Origination System (LOS)** and a **Loan Servicing
System (LSS)** bolted together behind a single API gateway, with a Next.js borrower +
servicing portal. (Lending Ops keeps asking for an "AI underwriting assistant" — that
work has not been started.)

Since the handoff the in-house team has begun **extracting the LOS monolith into focused
services**, partly to match the platform's intended target architecture. The platform now
runs **seven** backend services: the original three plus four extracted ones —
`kyc-service`, `decision-service`, `disclosure-service`, and `payment-service`.
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

All seven services share **one** Postgres database and the same `db/init` schema + seed —
the data layer is unchanged by the decomposition. The LOS↔LSS **seam** is still thin and
undocumented — a loan "boards" from origination to servicing by a direct insert into the
servicing schema. After a charge is captured, `payment-service` calls servicing's
`apply-payment` to post it. See `docs/architecture.md`.

## Quick start

```bash
cp .env.example .env     # the real .env is already in the repo so you can just run it
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

**Not PCI-DSS compliant** — the `payments` table still carries plaintext `pan` and `cvv`
columns (`db/init/001_schema.sql`), and `db/init`'s seed scripts still write card values
into them (`002_seed.sql`, `003_seed_bulk.sql`), so every freshly initialised database
contains card data. Storing CVV/SAD post-authorization is a flat PCI-DSS violation
independent of encryption; it predates the current engagement (vendor debt, see
`adr/0003`) and the columns are still there — tracked as `docs/DEBT.md` D5b/D13.

This section previously said that `payment-service` logs them at INFO and persists the PAN
and CVV itself. Both claims are false against the current code, and were verified against
it: `PaymentIn` sets `extra="forbid"` and accepts only a processor token plus
`last4`/`brand` (ADR 0008), so a field *named* `pan`, `cvv` or `ssn` is rejected with a 422
rather than dropped silently; its INSERT writes `last4`/`brand` and never the card number;
and `charge()` builds its log line through `redact_dict`, which masks sensitive keys and
runs the PAN/SSN/CVV patterns over every other string value — so card data pushed through
an *allowed* field (a PAN in `processor_token`, say) is redacted before it is logged, which
the schema alone would not prevent.
The remaining exposure is the schema and the seed data, not the application's write or log
path — see `docs/DEBT.md` D5a for the per-call-site logging verification.

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
