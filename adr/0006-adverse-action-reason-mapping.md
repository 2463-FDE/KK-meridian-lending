# ADR 0006: Feature-driven adverse-action reasons + append-only decision memory

- **Status:** Accepted
- **Date:** 2026-07-14
- **Author:** In-house team

## Context

Client message (Dana): licensed a new "more accurate" AI credit-scoring model. Wants
it wrapped in an assistant that decisions an application and tells the officer the
result, "clean and simple." Slows down under load but "accuracy is what matters."

Before wiring in the new scorer, we verified what the existing decisioning code
actually does (`services/decision-service/app/decision.py`):

- Every deny/refer emitted the identical hardcoded string, `GENERIC_REASONS[0]`
  ("purchasing history") — `GENERIC_REASONS[1]` was unreachable dead code. This isn't
  a "nearest checkbox" approximation of the model's real driver; it's unconnected to
  the model's inputs entirely.
- The model itself (`model_score = bureau_score * 0.9 + income/1000`) only ever used
  two inputs — bureau score and income. "Purchasing history" was never one of them.
- The `decisions` table stored `(app_id, outcome)` only. No inputs, no score, no
  model version, no reason, no timestamp. If a denied applicant disputed the reason,
  nothing in the system could prove what actually drove the decision.
- CFPB Circular 2023-03 confirms there is no AI-model exemption from ECOA/Reg B: an
  accurate score paired with an inaccurate stated reason is still a violation. "Move
  fast" on the new scorer cannot mean shipping its output with the same disconnected
  reason string.

## Decision

**1. The AI scorer is a fail-closed tool call, same contract as the bureau pull.**
`_call_ai_scorer()` mirrors `_pull_credit()`'s existing pattern (`CreditBureauUnavailableError`,
fixed as part of the RF-1 review on PR #3): a missing/unreachable licensed model
raises `ModelUnavailableError` outside dev/test, rather than silently substituting a
fake score. `ALLOW_MODEL_STUB` shares the same environment gate as `ALLOW_CREDIT_STUB`
(dev/test only), and the deterministic stub's return value is tagged with a `-stub`
suffix on `model_version` — an audit reader can always tell whether a given decision's
score came from the real vendor or a dev-time fallback.

**2. Adverse-action reasons map to whichever input actually drove the score down.**
`_reason_codes()` compares each factor's shortfall from a healthy baseline (720 bureau
score, $50k income — reference points for comparison, not approval thresholds) and
names the larger shortfall as the principal reason:
`REASON_LOW_BUREAU_SCORE` or `REASON_INSUFFICIENT_INCOME`. This is deliberately simple
(two factors, one comparison) — proportional to what a two-input model can actually
support. It replaces the fixed string with something a compliance reviewer can verify
against the model's real inputs, which is the actual Reg B requirement; it does not
attempt a full SHAP-style feature-attribution system, which the model's own inputs
don't justify.

**3. Every decision persists an append-only `decision_events` row** (new table,
`db/init/004_decision_events.sql`): `app_id`, `occurred_at`, `requested_amount`,
`term_months`, `annual_income`, `bureau_score`, `model_score`, `model_version`,
`top_features` (the contribution breakdown), `decision`, `reason_codes`. This is the
dispute-proof record the brief's own question exposed as missing. No raw SSN/PAN is
stored — inputs are limited to the fields the scorecard/model actually consume.

**4. Append-only is enforced with a trigger, not a GRANT.** Every service in this
project connects to Postgres as the same schema-owning role (ADR 0002, single shared
database) — a plain `REVOKE UPDATE, DELETE` doesn't bind a role that owns the table it
was granted on. `reject_decision_events_mutation()` is a `BEFORE UPDATE OR DELETE`
trigger that unconditionally raises, so mutation is rejected regardless of which role
issues it. Verified directly: `UPDATE`/`DELETE` against a live-inserted row both fail
with `decision_events is append-only: ... is not permitted`, even from the app's own
DB role. The legacy outcome-only `decisions` table is left as-is (still written on
every decision) since other code may still depend on its shape; `decision_events` is
additive, not a replacement, this week.

## Sync → async note

Not fixed this week (brief: move fast, not rearchitect) — flagging for the record.
The credit pull, the AI-scorer call, and now two additional inserts are all still a
synchronous chain on the request thread (load note: timeouts past ~20 concurrent
apps, unchanged since the service-decomposition ADR 0004). Adding a second external
call (the AI scorer) to that chain makes the latency/timeout exposure worse, not
better — the "slows down under load" complaint in the brief is this chain, and the
new scorer call lengthens it further. A real fix moves decisioning to an async
job/queue with the applicant polling or being notified on completion; that is
future-roadmap work, not in scope for this week's deliverable.

## Consequences

- **Pro:** a denied applicant's stated reason is now traceable to which of the
  model's two actual inputs drove the score down, and every decision has a
  dispute-proof, tamper-resistant record.
- **Pro:** a misconfigured/unreachable licensed scorer fails the request loudly
  instead of quietly grading applicants against fake data — and `/health` (extended
  this week) reports unhealthy before the stack takes traffic it can't correctly
  serve.
- **Con:** the reason mapping is only as sophisticated as the model's two inputs.
  If the licensed model later adds real features (debt, LTV, etc.), this mapping
  needs to grow with it — it is not a generic feature-attribution framework.
- **Con:** the synchronous chain is now longer and slower under load, not shorter.
  This ADR documents that risk; it does not fix it.
- **Con:** ~~`decision_events` and `decisions` are two separate writes with no
  transactional link between them...~~ **Fixed (review, same PR):** both inserts
  now go through one `db.transaction()` call (`app/db.py`) — they commit or roll
  back together, and `decide()` raises `DecisionPersistenceError` instead of
  swallowing a persistence failure, so a decision is never returned to the caller
  without the audit row that proves it happened. See `app/decision.py::decide()`
  and `tests/test_decision.py::test_decide_persists_decision_and_event_in_one_transaction_call`.

## Addendum (review fixes, same PR)

Three findings from review, all fixed before merge:

1. **Non-transactional dual write (above).** Fixed — one `db.transaction()` call,
   fail-closed on error.
2. **Reason codes could misattribute a real vendor score's driver.** `_reason_codes()`'s
   bureau/income shortfall formula is only true of the *stub* score, which is
   literally computed from that formula (`_stub_model_score`). A real licensed-model
   response also weighs `requested_amount`/`term_months`, which that formula knows
   nothing about — reporting a locally-guessed reason for a real vendor score risked
   naming a driver that wasn't actually why the model scored the applicant that way.
   Fixed: `_call_ai_scorer()` now requires `reason_codes` in a real vendor response
   and raises `ModelUnavailableError` (fails closed) if the vendor omits them, rather
   than falling back to the local heuristic. The heuristic remains authoritative only
   for the deterministic dev/test stub path, where it's known to be exactly correct.
3. **`decision_events` only existed in `db/init/`.** A persistent-volume deployment
   created before this PR would never pick up the new table (`db/init/*.sql` only
   runs on a *fresh* volume's first boot) — every `/decisions` call would then fail
   the transaction above. Fixed: added `db/migrations/0004_add_decision_events.sql`
   (mirrors `db/init/004_decision_events.sql`, hand-applied per this repo's existing
   migration convention), and `/health` now calls `_decision_events_ready()` to
   verify the table actually exists before reporting healthy, the same pattern
   already used for the `EXPERIAN_KEY`/`AI_MODEL_API_KEY` readiness checks above.
