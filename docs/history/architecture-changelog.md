# Architecture Changelog

> **Historical record.** This file keeps the corrections, superseded designs and earlier
> status wording that used to sit inline in [`ARCHITECTURE.md`](../../ARCHITECTURE.md) and the
> [README](../../README.md). The current system is described in those two documents; nothing here
> is a statement about the code as it stands today. Defect IDs (`D`, `RF`, `SEC`) refer to
> [`docs/DEBT.md`](../DEBT.md).

## Status labels retired from the legend

The status legend once carried two engagement-specific labels:

- **Fixed in PR #6**: a defect whose fix was on `kalab-week4-disclosure-automation` with a
  test that failed without it.
- **Closed by PR #8**: a defect PR #6 deliberately did not close and PR #8 did (merged
  2026-08-05). The label read "Still open for PR #8" while that was true.

Both are now covered by the general **Fixed** label: a defect in `docs/DEBT.md` whose fix has
a test that fails without it.

## Service decomposition timeline

- Halcyon delivered three backend services: gateway, origination (LOS) and servicing (LSS).
- KYC, decisioning, disclosure and payments were extracted into standalone services (ADR 0004).
- The AI loan assistant was added in Weeks 1-2 of the engagement.
- The knowledge-graph-style traversal of the shared schema (`kg.py`) landed in Week 4, for
  auto-disclosure and the fair-lending report.
- The Prometheus/Grafana metrics stack landed in Week 7. No cross-service metrics existed
  before it.
- `decision_events`, the append-only decision evidence record, landed in Week 3.

## Network boundary and the internal token

- **Host-published backend services.** Backend services used to publish host ports alongside
  the gateway's own authorization, which made its staff-only and ownership checks skippable
  by calling a service directly. Only `gateway` and `frontend` publish host ports now.
- **kyc-service.** It was the one service that was both host-published and unauthenticated,
  so `POST localhost:8003` wrote a CIP compliance record for any applicant id it named
  (verified against a running stack). The paragraph describing this read "Still open (not
  fixed in PR #6)" for two months: PR #8 shipped card tokenization instead, nothing
  reassigned the gap, and the regression test that would have caught it named four services
  by hand and omitted this one. The lesson recorded at the time: a partial enumeration in a
  security test reads exactly like a complete one. The host-publication test now derives its
  service list from `services/` on disk.
- **servicing-service money routes.** Not being host-published was once the only control on
  servicing's money routes; any container that could resolve `servicing-service:8002` could
  set a balance to zero. The paragraph read "`servicing-service` doesn't check the token
  either ... Both are tracked for PR #8"; PR #8 shipped card tokenization instead and closed
  neither. A fifth guarded route, the legacy `/payments` duplicate, was retired with D2.
- **Repository-supplied token.** A fallback token value was once committed to the
  repository. It was not a secret: the failure the token defends against (a port re-exposed,
  the network boundary bypassed) is precisely the one where an attacker can read the default
  out of the repository. Compose now requires the value and services refuse a
  repository-known one outside development.
- **Inbound `X-Internal-Token` stripping.** Header names arrive lowercased, so a client's
  `X-Internal-Token` once survived as a separate dict key from the one the gateway added; both
  reached the wire and the downstream `Header(alias=...)` read the client's. Any caller could
  force a 401 on every internal-token route (fail-closed, so availability rather than
  escalation, but caller-controlled).
- **Spoofed `X-User-Role`.** A caller used to be able to send `X-User-Role: admin` on an
  otherwise-anonymous route.
- **`/kyc/*` gateway tier.** `/kyc/*` was anonymous-allowed on the reasoning that an applicant
  applies without an account. That reasoning held for `/los/*`, which gates its own sensitive
  routes, but `/kyc/*` forwarded straight to a service whose only authentication was the token
  the gateway itself attached, so an anonymous caller could have the gateway sign a request
  that wrote a `kyc_checks` row for any `applicant_id`.

## Payment preflight: from fail-open to a real write

- The first version of the servicing preflight failed **open**, on the reasoning that
  "unknown is not known-bad" and that refusing payments during every servicing blip traded a
  rare accounting error for a common outage. It caught only an explicit 401; servicing simply
  being down sailed past it. The fallback argument, that the reconciler drains
  captured-but-unapplied rows, only holds once servicing returns.
- An intermediate version authenticated and returned without touching the database, so a
  servicing process that was up with its database down answered 200, the card was captured,
  and the follow-up `apply-payment` failed.
- A later version wrote to a dedicated `preflight_writes` table. That was the same defect one
  level down: per-table grant drift or a trigger failure on either money table left the probe
  table healthy. The preflight now writes to the tables `apply_payment_once` writes.

## D8 status wording

`ARCHITECTURE.md` first described all of D8 as open long after two thirds of it had landed,
and was then corrected to "partly closed: servicing validates no human principal and no
second approver exists". That correction was itself out of date within a week: both halves
landed in PRs #33-#35. The verified principal was introduced by PR #33, PRs #34/#35 added
maker-checker (`db/migrations/0036`/`0037`), and PR #81 extended the principal check to the
maker-checker queue and the reconciliation review queue. `db/tests/test_d8_is_closed_everywhere.py`
fails on either superseded wording.

Until 2026-08-24, `ARCHITECTURE.md` also stated that every downstream service trusted the
forwarded `X-User-Role` without re-checking it, which understated the control servicing
already had.

## Corrections to the service table

- **origination-service.** The row once claimed a request-logging middleware in
  `logging_config.py` logged full POST bodies. No such middleware has ever existed (D5c).
- **origination-service, fair lending.** Origination drove a ZIP3 four-fifths-rule screen
  until 2026-08-24, when the client prohibited any protected-class proxy, including one
  derived from ZIP. The module was deleted rather than disabled (PR #78), and `kg.py` no
  longer references ZIP. `applicants.zip_code` was added in Week 8 to back that screen; the
  column stays because an address without a ZIP is an incomplete address.
- **servicing-service.** The row described `apply-payment` as writing to "still a single
  mutable column, no ledger" after the ledger projection shipped (ADR 0010,
  `db/migrations/0035`). That was a wording variant of a claim corrected elsewhere in the same
  file, which is why the regression pins match the concept rather than one phrasing.
- **kyc-service.** The row read "Host-published and does not check `X-Internal-Token`",
  which was the bypass described above.
- **payment-service.** Idempotency landed in PR #6; the PCI debt (raw PAN/CVV capture) was
  closed in PR #8 by client-side tokenization (ADR 0008, superseding ADR 0003).
- **loan-assistant.** The row once said traces were "PII-scrubbed by the same guardrails".
  That was measured and was false: the guardrails run on what the summary returns, while the
  tracer serialised the whole run. Policy chat was listed as anonymous-allowed on the
  reasoning that the content carries no applicant data, which was true of the content and
  silent about the audience; the client then made it a staff tool (RF-28).
- **prometheus / grafana.** The claim that LangSmith "only ever covered loan-assistant's own
  LLM calls" was wrong: `decision-service` and `origination-service` both run LangGraph, which
  auto-instruments through `langchain-core` whenever `LANGSMITH_TRACING` is set, and both were
  exporting graph state. Both now suppress it.

## Data model corrections

- Every dollar-amount column used to be `DOUBLE PRECISION` (D12); money columns are `NUMERIC`.
- `balances` was a single mutable column overwritten by its last writer before
  `db/migrations/0035` made it a projection of `ledger_entries`.
- `offers.decision_id` used to trust a caller-supplied value verbatim, which leaked a decision
  for an unrelated application.
- `payments.pan`/`cvv` survived as nullable legacy columns until `db/migrations/0031` dropped
  them. The writers went first (PR #8, PR #11) and `db/migrations/0029` back-filled `last4`.
- The LOS-to-LSS paragraph read "the balance-mutation debt (race / lost-update, mutable
  balance, no payment waterfall, no maker-checker) lives behind that endpoint and is
  unchanged" until D3, D14 and D8 landed.

## CI corrections

- `|| true` once masked every backend test failure, including a missing pytest install.
- A Dockerfile once built only on a developer machine with local state (a CA-bundle copy);
  the `docker compose build` smoke test exists because that shipped once.
- The dependency-audit findings were triaged on 2026-09-01 (`docs/DEBT.md` SEC-11); the CI
  section previously said they were not.
