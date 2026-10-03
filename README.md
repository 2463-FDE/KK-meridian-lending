# Meridian Lending Platform

A consumer-lending platform covering loan origination, credit decisioning, disclosures, servicing, payments and reconciliation, built as a **Forward Deployed Engineering** brownfield project.

The codebase started as a vendor-delivered monolith (a loan origination system and a loan servicing system behind one gateway). The in-house team hardened it and decomposed it into **eight FastAPI backend services, including the gateway**, behind a Next.js borrower and staff portal, and added a staff-only RAG policy assistant.

> **Local training/demo platform.** It runs with `docker compose` against seeded fictional data and mocked external services. There is no production environment, no real applicant data, no real credit bureau and no real card rails. No production, regulatory, PCI-DSS or other compliance certification is claimed. Naming a regulation (TILA, ECOA/Reg B, PCI-DSS) identifies the rule a control is modelled on, not a compliance status. The status labels used across the docs are defined in [ARCHITECTURE.md](ARCHITECTURE.md#status-legend).

## What it demonstrates

- **Brownfield modernization**: tracing an inherited system, extracting services from the origination monolith, and recording each decision as an ADR ([`adr/`](adr/), twelve of them)
- **Money-handling controls**: idempotent payment capture, an append-only servicing ledger, maker-checker approval of balance adjustments and fee waivers, and scheduled payment reconciliation with a human review queue
- **Auditable credit decisions**: every decision writes an append-only evidence record (inputs, model version, score, reason codes), and manual-review outcomes are stored separately so history is never overwritten
- **TILA / APR disclosures** checked against independent golden payment-schedule vectors ([`db/golden/`](db/golden/))
- **Security and observability**: gateway authentication and rate limiting, a fail-closed internal service token, card-data redaction, secret scanning in CI, and Prometheus alert rules
- **A bounded, advisory AI assistant** that cannot make or change a lending decision

## Architecture

```
 Next.js portal ─────► gateway (BFF)  :8000   session auth, roles, rate limiting
                           │  /auth · /los · /lss · /kyc · /assistant
                           │  /decision · /disclosure · /payments
        ┌──────────────────┼────────────────────────────────┐
        ▼                                                    ▼
 origination-service :8001                          servicing-service :8002
 intake + boarding orchestrator                     balances, ledger, maker-checker,
        │  synchronous HTTP                         delinquency, reconciliation
        ├─► kyc-service         :8003                          ▲
        ├─► decision-service    :8004                          │ apply-payment
        └─► disclosure-service  :8005                 payment-service :8006

 loan-assistant :8007   staff-only RAG policy assistant, read-only, no database connection

 Postgres :5432 (shared by seven services) · Redis :6379 (sessions)
```

The platform now runs **eight** backend services, including the gateway. Seven of them share one PostgreSQL schema under an explicit decision ([ADR 0002](adr/0002-single-database-shared-schema.md)). `loan-assistant` is the exception: it holds no database connection and reads application data from origination-service over HTTP. The `reconciliation` container in `docker-compose.yml` is the servicing image running a scheduled job, not a ninth service. The decomposition is partial; remaining debt is tracked in [`docs/DEBT.md`](docs/DEBT.md).

| Path | Service | Port | Responsibility |
|------|---------|------|----------------|
| `frontend/` | Next.js 15 portal | 3000 | application wizard, servicing dashboard, staff views |
| `services/gateway/` | FastAPI BFF | 8000 | session auth and roles, rate limiting, routing |
| `services/origination-service/` | FastAPI | 8001 | intake and loan boarding; orchestrates KYC, decision and disclosure |
| `services/servicing-service/` | FastAPI | 8002 | balances, schedule, ledger, maker-checker, delinquency, reconciliation |
| `services/kyc-service/` | FastAPI | 8003 | identity verification |
| `services/decision-service/` | FastAPI | 8004 | credit pull and scoring; compute-only, persists nothing |
| `services/disclosure-service/` | FastAPI | 8005 | TILA offer, APR and amortization |
| `services/payment-service/` | FastAPI | 8006 | tokenized card/ACH charge; posts to servicing |
| `services/loan-assistant/` | FastAPI + LangChain | 8007 | advisory RAG policy assistant |

## Credit decisions and the AI assistant

These are two separate things, and only one of them makes decisions.

1. **Credit decision** (`decision-service`): pulls a credit report and calls an external AI scoring model, with thresholds mapped to approve, refer or decline and reason codes for adverse action. It **fails closed** when the scorer or bureau is unavailable. A deterministic stub is available only in non-production environments, and its output is labelled as such. Origination writes the decision and its evidence record; decision-service itself stores nothing. See [`docs/model_card.md`](docs/model_card.md).
2. **RAG policy assistant** (`loan-assistant`): **staff-only and advisory**. Both routes, `/assistant/policy-chat` and `/assistant/applications/{id}/summary`, are staff-only (lending, compliance and underwriting staff). It answers lending-policy questions and summarizes an application for staff, using one bounded read-only tool over an approved policy corpus. It refuses to answer when retrieval returns no policy evidence, never writes to any system, and its summaries are labelled "not a decision". Corpus hygiene is covered by [ADR 0005](adr/0005-rag-corpus-hygiene.md).
3. **System of record**: origination-service and the Postgres decision tables. The assistant's output never changes them.

## Security and card data

- Session authentication and role checks at the gateway; services accept calls only with an internal service token, and in non-development environments a weak or missing token is rejected rather than defaulted
- **No card data is stored.** Capture is tokenized in the browser ([ADR 0008](adr/0008-tokenize-card-data-stop-storing-pan-cvv.md)); the payment service receives a processor token plus `last4` and brand, rejects raw PAN, CVV or SSN fields, and redacts sensitive patterns before logging. The earlier plaintext `payments.pan` and `payments.cvv` columns were dropped by migration `0031`. This closes a specific defect; it is **not** a PCI-DSS position, since this build has a mocked processor and no assessment. The full trace is in [`docs/PAN-CVV-DATA-FLOW.md`](docs/PAN-CVV-DATA-FLOW.md)
- Secret scanning (gitleaks) on every CI run, and dependency audits
- The loan assistant has no database connection, so no applicant row is reachable from the agent's process

## Reliability and observability

- Idempotency keys on payment capture and a settlement-comparison reconciliation job that flags breaks for human review
- Append-only ledger for servicing balances ([ADR 0010](adr/0010-append-only-ledger-for-servicing-balances.md)) and maker-checker approval for adjustments ([ADR 0011](adr/0011-maker-checker-for-servicing-adjustments.md))
- Structured logging with correlation IDs, request tracing across the decision chain, and Prometheus alert rules ([`monitoring/`](monitoring/))

## Testing and CI

`.github/workflows/ci.yml` runs on pull requests and on pushes to `main`:

- **Secret scan** with gitleaks
- **Backend**: a Pytest job for each of the eight services, with PostgreSQL where tests need it
- **Database migrations** tested against a real Postgres, including a test that checks the README's card-data claims against the actual schema
- **Frontend** build, and a **Playwright end-to-end** run of the borrower workflow against the full Docker Compose stack
- **Quick-start and Docker build checks** that prove a clean checkout refuses to start without a generated token and that every image builds
- Dependency audits (non-blocking; triaged in [`docs/DEBT.md`](docs/DEBT.md))

Run the service suites locally with `make test`.

## Run locally

```bash
make bootstrap    # creates .env and generates a local INTERNAL_SERVICE_TOKEN (never commit it)
make up           # postgres, redis, all services and the frontend
make seed         # load the seed data
make logs         # tail everything
make down
```

Portal: http://localhost:3000 · Gateway API docs: http://localhost:8000/docs

Synthetic staff and borrower accounts are seeded for local testing; the demo access details are in [`docs/runbook.md`](docs/runbook.md#demo-logins), which also covers health checks and resetting the database.

## Documentation

| Document | What it covers |
|----------|----------------|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) and [`docs/architecture.md`](docs/architecture.md) | System shape, auth and roles, data model, the origination/servicing seam |
| [`adr/`](adr/) | The twelve architecture decision records |
| [`docs/DEBT.md`](docs/DEBT.md) | Known defects and technical debt, with status and history |
| [`docs/model_card.md`](docs/model_card.md) | The scoring model and its limits |
| [`docs/PAN-CVV-DATA-FLOW.md`](docs/PAN-CVV-DATA-FLOW.md) | Where card data goes, and what stops it being stored |
| [`docs/runbook.md`](docs/runbook.md) | Operating and local-development guide |
| [`docs/ROADMAP.md`](docs/ROADMAP.md) | The plan the ADRs and code comments refer to |
