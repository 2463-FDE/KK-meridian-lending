# Debt register

The `D`- and `RF`- numbers cited throughout this repository's code comments,
ADRs and runbook. They were being cited before they were written down anywhere,
so a reader hitting "(debt D7)" in a source file had no way to find out what D7
was. This file is that lookup.

**Numbering.** `D<n>` are defects inherited from the Halcyon handoff or found
while reading the code. `RF-<n>` are review findings raised during this
engagement. Neither sequence is contiguous — gaps are numbers that were assigned
during review and later folded into another entry, and renumbering them would
break the citations already committed in code.

**Status** uses the labels from [ARCHITECTURE.md § Status
legend](../ARCHITECTURE.md#status-legend). "Fixed" means there is a test that
fails without the fix, not that it looked right on inspection.

Every entry below was written from the citation site in the code, not from
memory. The "Cited at" column is where to look for the current detail.

## Defects

| ID | What | Status | Cited at |
|---|---|---|---|
| **D1** | Money math done in float rather than Decimal on the disclosure read path. Partly addressed — the offer/APR/schedule write paths compute in Decimal now, but the read path still rebuilds the display schedule in float. | Partly fixed | `disclosure-service/app/routers/offers.py` |
| **D2** | `POST /payments` had no idempotency key, so a retried request inserted a second row and applied the balance twice. | **Fixed** — required `idempotency_key` + partial unique index (`db/migrations/0007`), atomic `ON CONFLICT DO NOTHING`, apply-once in `payment_applications` (`0013`) | `payment-service/app/main.py` |
| **D3** | `balance.apply_payment` is a read-modify-write with no row lock — two concurrent payments can lose one update. | **Open** | `servicing-service/app/balance.py` |
| **D4** | `decisions` stored outcome only: no reason, no score, no inputs, no timestamp, for any application ever. | **Fixed** — append-only `decision_events` (Week 3, ADR 0006), trigger-enforced | `decision-service/app/models.py` |
| **D5** | PII in logs and in storage. Two halves, tracked together because they were reported together. | **Split** — see below | `ARCHITECTURE.md`, `payment-service/app/main.py` |
| D5a | Card/PII written to logs at INFO with no redaction. | **Fixed in payment-service** (`redactor.py`); **still open** in `servicing-service`'s legacy `POST /payments`, and in origination's request-logging middleware, which still logs the full POST body | `servicing-service/app/payments.py`, `origination-service/app/logging_config.py` |
| D5b | Full PAN/CVV persisted on the `payments` row. | **Open on `main`** — closed on the Week 5 tokenization branch (ADR 0008), not yet merged | `db/init/001_schema.sql` |
| **D6** | The origination fee constant had drifted to three independent copies, one of them `0.025` against a published 3.0%, breaching the Reg Z APR tolerance. This is the real Reg Z breach — not the arithmetic. | **Fixed** — one source of truth in `disclosure-service/app/fees.py` | `disclosure-service/app/fees.py` |
| **D7** | `reconciliation.peek` exposes two totals that do not tie out. It is not a control — nothing runs on a schedule and nothing alerts. | **Open** | `servicing-service/app/main.py` |
| **D8** | Fee waiver / balance adjust is available to *any* authenticated user — no role check, no second approver, no ledger entry. | **Open** | `servicing-service/app/main.py` |
| **D11** | KYC is CIP-only: no OFAC/sanctions screening, no UBO, no ongoing monitoring, no SAR path. | **Open** (deliberate scope limit, not an oversight) | `kyc-service/app/main.py` |
| **D12** | Money columns stored as `DOUBLE PRECISION`, accumulating float error across the amortization loop. | **Fixed** — migrated to `NUMERIC`, Decimal arithmetic in `apr.py`/`offer.py`/`schedule.py` and `balance.py`/`delinquency.py`/`schedule.py` | `docs/runbook.md` |
| **D13** | Full PAN/CVV storage, PCI. Same defect as D5b, numbered separately in the original report. | **Open on `main`** — see D5b | `payment-service/app/main.py` |
| **D14** | No payment waterfall — a payment reduces the balance directly instead of being applied fees → interest → principal. | **Open** | `servicing-service/app/balance.py` |
| **D15** | `compute_apr` was not the actuarial method despite its docstring saying so — it used a simple add-on ratio, understating the disclosed APR by 4.39pp on the repo's own vector (5.196% against an actuarial 9.584%), 35× the Reg Z tolerance, in the direction that flatters the loan. Found by giving the tests an oracle that does not re-implement `apr.py`. | **Fixed** — actuarial IRR solve (Reg Z Appendix J); `test_apr.py` checks it against an independently-solved rate, a no-fee identity that needs no implementation, and hand-checkable arithmetic | `disclosure-service/app/apr.py` |
| **D16** | The disclosed finance charge excluded the origination fee (a prepaid finance charge under Reg Z), so the TILA box did not foot: amount financed + finance charge was short of total of payments by exactly the fee. | **Fixed** — finance charge computed as `total_of_payments - amount_financed`; the identity is asserted per vector | `disclosure-service/app/apr.py` |
| **D17** | The offer read path rebuilt the display amortization schedule at the **APR** rather than the note rate. The two differ once a prepaid fee exists, so the redisplayed schedule showed a monthly payment that did not match the disclosed one. Present before D15 too, in the other direction. | **Fixed** — the note rate is recovered from the stored payment (`note_rate_from_payment`) and the schedule is built at that | `disclosure-service/app/routers/offers.py` |

## Review findings

| ID | What | Status | Cited at |
|---|---|---|---|
| **RF-1** | A missing or unreachable licensed model must fail closed rather than fall back to a stub score that could be mistaken for a real vendor response. | **Fixed** (PR #3) — `ModelUnavailableError`, `-stub` suffix on any stub `model_version` | `adr/0006-adverse-action-reason-mapping.md` |
| **RF-13** | Float-money test failures in disclosure-service and servicing-service, which CI had been routing around with a "known-flaky" split. | **Fixed** — see D12; the split is gone and all eight services run in one blocking job | `.github/workflows/ci.yml` |
| **RF-18** | `decisions` is `(app_id, outcome)` with no explanation for any application, and the RAG corpus dump carried raw `ssn`/`pan` on every record. | **Fixed** — `decision_events` (see D4) and corpus hygiene (ADR 0005) | `adr/0005-rag-corpus-hygiene.md` |
| **RF-21** | The embedding provider needed the same swappable-client shape planned for the credit bureau, so a vendor can be substituted without touching retrieval or eval code. | **Fixed** — that bureau client now exists as `decision-service/app/bureau.py` (`BureauClient` Protocol) | `adr/0005-rag-corpus-hygiene.md` |

## Not in this register

Two things are deliberately absent, so their absence is not mistaken for
coverage:

- **No severity ranking.** Assigning one would need a data-flow audit
  (which fields, reaching which sinks, readable by whom) that has not been
  done. An unranked list is more honest than an invented ordering.
- **No target dates.** This is a local training build with no production
  deployment and no on-call, so a due date here would be decoration.
