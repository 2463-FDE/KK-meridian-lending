# Meridian Lending

**Consumer Lending & Applied AI Platform.** A brownfield consumer-lending system modernized into FastAPI services behind a Next.js portal, covering the credit workflow (origination, KYC, credit decisioning, TILA disclosures) through servicing, payments and reconciliation, with a staff-only advisory AI layer (RAG, LangGraph, a LangChain agent on AWS Bedrock, LangSmith tracing) that never holds credit authority.

## What it demonstrates

- **Brownfield modernization**: an inherited origination/servicing monolith traced, hardened and decomposed into services, with each decision recorded as an ADR ([`adr/`](adr/))
- **Distributed FastAPI services** behind a gateway/BFF that owns sessions, RBAC and rate limits, with a fail-closed internal service token and correlation IDs across calls
- **Money-movement controls**: idempotent payment capture, an append-only servicing ledger, maker-checker approval and a scheduled reconciliation job with a human review queue
- **Auditable credit decisions**: decision finality and an append-only `decision_events` evidence record (inputs, model version, score, reason codes)
- **Applied AI with a hard boundary**: LangGraph orchestration for decisioning and disclosure assembly, plus a staff-only RAG policy assistant and LangChain agent on AWS Bedrock that is advisory only and cannot write to any system
- **Security and observability**: tokenized cards with no stored PAN/CVV, PII redaction before logs and LLM prompts, Prometheus and Grafana metrics, privacy-safe LangSmith tracing
- **Tested claims**: per-service pytest suites, migration tests against real PostgreSQL, a Playwright end-to-end run against the full Compose stack, and documentation guard tests

## Architecture

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/meridian-architecture-dark.svg">
    <img src="docs/meridian-architecture.svg" alt="Meridian Lending logical architecture. Borrowers and staff use the Next.js portal, which sends web/API requests to the gateway BFF (session auth, RBAC, rate limits; routes /auth, /los, /lss, /kyc, /decision, /disclosure, /payments, /assistant). Inside the private Compose network, origination-service is the system of record and makes internal calls to kyc-service, decision-service (a LangGraph state graph) and disclosure-service (driven by a two-agent LangGraph workflow); servicing-service holds the append-only ledger, maker-checker and reconciliation, and payment-service applies captured payments to it. Seven services share one PostgreSQL schema; Redis holds gateway sessions and rate limits. A staff-only advisory AI lane holds loan-assistant (RAG policy chat and a LangChain agent on AWS Bedrock with one bounded read-only policy tool), which reads origination read-only and has no database connection. Operations: Prometheus, Grafana, LangSmith tracing and CI." width="1000">
  </picture>
</p>

The repository runs locally on Docker Compose. Application services communicate over the private Compose network; production edge TLS is a deployment concern and is not claimed by the local stack.

<details>
<summary>Text version</summary>

```
 Borrower / Staff ──► Next.js portal ──► gateway (BFF)   session auth, RBAC, rate limits
                                           │  /auth · /los · /lss · /kyc · /assistant
                                           │  /decision · /disclosure · /payments
        ┌──────────────────────────────────┼──────────────────────────────┐
        ▼                                  ▼                              ▼  staff-only
 origination-service                servicing-service              loan-assistant  (advisory)
 system of record, intake,          append-only ledger,            RAG policy chat + LangChain
 boarding, two-agent LangGraph      maker-checker, delinquency,    agent on AWS Bedrock, one
 disclosure workflow                reconciliation                 bounded read-only policy tool
        │  internal call                   ▲ apply-payment                │ read-only (HTTP)
        ├─► kyc-service                    │                              └──► origination-service
        ├─► decision-service (LangGraph)   payment-service
        └─► disclosure-service

 PostgreSQL 16 (one schema, seven services) · Redis 7 (gateway sessions, rate limits)
 Operations: Prometheus · Grafana · LangSmith tracing · CI
```

</details>

The platform runs **eight** backend services, including the gateway. Seven of them share one PostgreSQL schema under an explicit decision ([ADR 0002](adr/0002-single-database-shared-schema.md)). `loan-assistant` is the exception: it holds no database connection and reads application data from origination-service over HTTP. The `reconciliation` container in `docker-compose.yml` is the servicing image running a scheduled job, not a ninth service. The full architecture, including auth tiers, the data model and the origination/servicing seam, is in [`ARCHITECTURE.md`](ARCHITECTURE.md).

| Path | Service | Responsibility |
|------|---------|----------------|
| `frontend/` | Next.js 15 portal | application wizard, servicing dashboard, staff views |
| `services/gateway/` | FastAPI BFF | session auth and roles, rate limiting, routing, signed principal for servicing |
| `services/origination-service/` | FastAPI + LangGraph | intake and loan boarding; system of record for decisions; orchestrates KYC, decision and disclosure |
| `services/servicing-service/` | FastAPI | balances, schedule, ledger, maker-checker, delinquency, reconciliation |
| `services/kyc-service/` | FastAPI | identity verification (CIP) |
| `services/decision-service/` | FastAPI + LangGraph | credit pull and scoring; compute-only, persists nothing |
| `services/disclosure-service/` | FastAPI | TILA offer, APR and amortization |
| `services/payment-service/` | FastAPI | tokenized card/ACH charge; posts to servicing |
| `services/loan-assistant/` | FastAPI + LangChain | advisory RAG policy assistant and underwriting-summary agent |

### Key design decisions

- **Credit authority stays deterministic.** `decision-service` runs a LangGraph state graph (pull credit, score, finalize) with threshold-mapped outcomes and reason codes, and **fails closed** when the scorer or bureau is unavailable. Origination writes the decision and its evidence record after a finality recheck. No language model touches the decision or the regulated money math. See [`docs/model_card.md`](docs/model_card.md).
- **AI is advisory and staff-only.** `loan-assistant` serves `/assistant/policy-chat` and `/assistant/applications/{id}/summary`, both staff-only at the gateway. It answers from an approved policy corpus, refuses when retrieval finds no policy evidence, and labels summaries "not a decision". The disclosure workflow in origination is a two-agent LangGraph (`kg_reader`, then `assemble_disclosure`) built from deterministic orchestration nodes, not model calls.
- **One shared schema, deliberately.** Seven services share PostgreSQL rather than splitting databases mid-modernization ([ADR 0002](adr/0002-single-database-shared-schema.md), [ADR 0004](adr/0004-decompose-origination-into-services.md)). The trade-off is coupling at the schema; remaining decomposition debt is tracked in [`docs/DEBT.md`](docs/DEBT.md).
- **Accounting correctness over availability.** Card capture preflights servicing with a real, rolled-back write and refuses the charge if servicing cannot accept it, so a captured payment is never left without a credit.

## Engineering highlights

- **Payments**: an idempotency key is required on every charge and backed by a partial unique index; servicing applies each payment once, through a fees, interest, principal waterfall
- **Ledger**: servicing balances are a projection of immutable `ledger_entries`, with append-only behaviour enforced by database triggers ([ADR 0010](adr/0010-append-only-ledger-for-servicing-balances.md))
- **Maker-checker**: balance adjustments and fee waivers create proposals that move nothing until a different approver resolves them; self-approval is refused, including for admin ([ADR 0011](adr/0011-maker-checker-for-servicing-adjustments.md))
- **Reconciliation**: a scheduled job compares the settlement file with captured payments, exits non-zero on breaks, and routes them to a human review queue
- **Disclosures**: TILA offer, APR and amortization checked against golden payment-schedule vectors ([`db/golden/`](db/golden/))
- **RAG**: a local retriever over the approved policy corpus, with corpus hygiene ([ADR 0005](adr/0005-rag-corpus-hygiene.md)) and a retrieval evaluation harness (`services/loan-assistant/app/rag_eval.py`)
- **Agent**: LangChain `create_agent` over `ChatBedrockConverse` (AWS Bedrock) with one bounded, read-only policy tool, a cost guard on input tokens, and fail-closed behaviour when the model is unavailable

## Security & Trust Boundaries

- **Gateway as the application entry point.** Backend services publish no host ports and are reachable only on the private Compose network. The gateway resolves sessions, enforces per-route RBAC (`csr`, `underwriter`, `admin`, `borrower`), applies rate limits, and strips any identity or internal-token headers a caller supplies.
- **Service-to-service authentication.** Services accept internal calls only with an `INTERNAL_SERVICE_TOKEN`, compared in constant time; outside development, a missing or repository-known token stops the service from starting.
- **Verified human principal for money movement.** The gateway signs an Ed25519 principal assertion for `/lss`; servicing verifies it on every money route, the maker-checker queue and the reconciliation review queue, so the internal token alone never authorizes a money movement.
- **Tamper-evident records.** The servicing ledger and `decision_events` are append-only, enforced by database triggers, and a final decision is not rewritten.
- **No card data stored.** Capture is tokenized in the browser ([ADR 0008](adr/0008-tokenize-card-data-stop-storing-pan-cvv.md)); payment-service receives a processor token plus `last4` and brand, rejects raw PAN, CVV or SSN fields, and the `payments` table has no PAN or CVV column. See [`docs/PAN-CVV-DATA-FLOW.md`](docs/PAN-CVV-DATA-FLOW.md).
- **AI advisory boundary.** The loan assistant is staff-only, has no database connection, reads application data from origination read-only, redacts PII before any prompt, and can neither make nor change a lending decision. LangSmith tracing carries metadata only.
- **Supply chain.** gitleaks secret scanning on every CI run, plus dependency audits.

Auth tiers and the reasoning behind each control are in [`ARCHITECTURE.md`](ARCHITECTURE.md#auth--roles).

## Reliability & Observability

- Idempotent payment capture, apply-once servicing, and reconciliation that treats a run that compared nothing as an error
- Synchronous internal calls with bounded client timeouts; the payment path fails closed rather than capturing money servicing cannot record
- Prometheus scrapes `/metrics` from all backend services, with alert rules and Grafana dashboards in [`monitoring/`](monitoring/)
- Structured logs with correlation IDs that follow one payment across services ([runbook](docs/runbook.md#following-one-payment-across-services))
- LangSmith tracing for the AI path, propagated from the gateway and built from an allow-list of categorical fields (`services/gateway/app/agent_trace.py`, `services/loan-assistant/app/trace.py`)

## Testing

`.github/workflows/ci.yml` runs on pull requests and on pushes to `main`:

- **Secret scan** with gitleaks
- **Backend**: a pytest job for each of the eight services, with PostgreSQL where tests need it
- **Database migrations** tested against a real PostgreSQL 16, including checks that the README's card-data claims match the actual schema
- **Frontend** build, and a **Playwright end-to-end** run of the borrower workflow against the full Docker Compose stack
- **Quick-start and Docker build checks** proving a clean checkout refuses to start without a generated token and that every image builds
- **Documentation guard tests** that fail when a document claims something the code does not do, including this README's service table and diagrams
- Dependency audits (non-blocking; triaged in [`docs/DEBT.md`](docs/DEBT.md))

Run the service suites locally with `make test`.

## Technology

| Area | Stack |
|------|-------|
| Backend | Python 3.12, FastAPI, Pydantic, httpx, SQLAlchemy, psycopg2 |
| Frontend | Next.js 15, React 19, TypeScript |
| Data | PostgreSQL 16, Redis 7 |
| AI | LangChain v1, `langchain-aws` (AWS Bedrock), LangGraph, local policy retriever, LangSmith tracing |
| Operations | Docker Compose, Prometheus, Grafana, structured logging |
| Quality | pytest, Playwright, GitHub Actions, gitleaks, pip-audit, npm audit |

## Project scope

A brownfield Forward Deployed Engineering engagement on an inherited consumer-lending platform; it runs locally with synthetic lending data and simulated integrations (no real applicant data, credit bureau or card rails), and no production, regulatory, PCI-DSS or other compliance certification is claimed. Naming a regulation (TILA, ECOA/Reg B, PCI-DSS) identifies the rule a control is modelled on. The repository models the application architecture and local service boundaries; the controls a production deployment would add are listed in [ARCHITECTURE.md](ARCHITECTURE.md#deployment-considerations). Status labels used across the docs are defined in [ARCHITECTURE.md](ARCHITECTURE.md#status-legend).

## Run locally

```bash
make bootstrap    # creates .env and generates a local INTERNAL_SERVICE_TOKEN (never commit it)
make up           # postgres, redis, all services and the frontend
make seed         # load the seed data
make logs         # tail everything
make down
```

Portal: http://localhost:3000 · Gateway API docs: http://localhost:8000/docs · Grafana: http://localhost:3001

Synthetic staff and borrower accounts are seeded for local testing; the demo access details are in [`docs/runbook.md`](docs/runbook.md#demo-logins), which also covers health checks and resetting the database. Service ports are listed in [`ARCHITECTURE.md`](ARCHITECTURE.md#services).

## Documentation

| Document | What it covers |
|----------|----------------|
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Current architecture: services, auth and roles, data model, the origination/servicing seam |
| [`adr/`](adr/) | Architecture decision records |
| [`specs/`](specs/) | Specifications for idempotent payments, maker-checker, fair-lending monitoring and KYC/AML |
| [`docs/model_card.md`](docs/model_card.md) | The scoring model and its limits |
| [`docs/PAN-CVV-DATA-FLOW.md`](docs/PAN-CVV-DATA-FLOW.md) | Where card data goes, and what stops it being stored |
| [`docs/runbook.md`](docs/runbook.md) | Operating and local-development guide |
| [`docs/diagrams/generate_diagrams.py`](docs/diagrams/generate_diagrams.py) | Generator for the architecture diagram |

**History.** [`docs/history/inherited-architecture.md`](docs/history/inherited-architecture.md) is the architecture baseline reconstructed when the codebase was inherited, and [`docs/history/architecture-changelog.md`](docs/history/architecture-changelog.md) records the corrections and superseded designs behind the current architecture. [`docs/DEBT.md`](docs/DEBT.md) (the debt register that `D`/`RF`/`SEC` IDs in code comments point to), [`docs/ROADMAP.md`](docs/ROADMAP.md) and the dated decks in [`docs/presentations/`](docs/presentations/) are engagement records; their status labels reflect that engagement.
