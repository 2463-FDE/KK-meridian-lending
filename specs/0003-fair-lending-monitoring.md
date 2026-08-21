# Spec 0003 — Fair-lending monitoring: denial-reason accuracy and disparity

- **Status:** Accepted
- **Domain:** Decisioning · Origination
- **Authority for the requirement:** Week 8 client brief (Dana, VP Lending Ops)
  — *"a fair-lending monitoring spec (denial-reason accuracy + disparity
  check)"*, delivered as a governance artifact, explicitly **not** a model
  rebuild.
- **Regulatory anchor:** [12 CFR 1002.9](https://www.ecfr.gov/current/title-12/chapter-X/part-1002/section-1002.9)
  (Regulation B, adverse-action notification) — a statement of the **specific**
  principal reasons for the action taken.
  CFPB Circulars 2022-03 and 2023-03 said the same thing and were **withdrawn on
  2025-05-12**; they are named here only so nobody re-cites them. See
  [`adr/0006-adverse-action-reason-mapping.md`](../adr/0006-adverse-action-reason-mapping.md),
  *Addendum (2026-08-06)*, where exactly that mistake was already made once.

## Context — what exists on `main` today

The client's brief describes a system that no longer exists in three respects,
and saying so is part of the spec rather than a footnote: `GENERIC_REASONS` was
removed in Week 3, a model card exists, and `decision_events` already records
the model version and features behind every decision. The attached decision logs
are stale. What follows is measured against the code, not the brief's
description of it.

| Concern | Where it lives | State |
|---|---|---|
| Reason from the real vendor | `services/decision-service/app/decision.py` | vendor's `reason_codes` used **verbatim**; missing, empty, or non-list fails closed |
| Reason from the deterministic stub | `decision.py::_reason_codes` | derives from the larger of two shortfalls (bureau vs income); dev/test only, gated by `ALLOW_MODEL_STUB`, `model_version` suffixed `-stub` |
| Per-decision audit record | `decision_events` (`db/init/004_decision_events.sql`) | append-only by DB trigger; carries `model_version`, `model_score`, `top_features`, the inputs, `decision`, `reason_codes`, `occurred_at` |
| Outcome disparity | `services/origination-service/app/fair_lending.py` | ZIP3 approval rate, four-fifths screen against the highest rate, groups under `min_group_size=5` excluded from flagging; staff-only `GET /applications/fair-lending/zip-analysis` |
| Aggregate monitoring over time | — | does not exist; `docs/model_card.md` says so under *Monitoring* |

## Problem

Two questions have no answer today, and they are different questions.

1. **Are the reasons accurate?** Reg B requires the specific principal reason.
   Nothing measures whether the reason a consumer receives corresponds to the
   driver that actually moved the decision, and nothing reports how many
   *distinct* reasons the system emits — the brief's first question, which today
   is answerable only by reading source.
2. **Is the model fair?** The ZIP3 screen measures **outcomes**, not the model.
   It cannot support a statement about the model, and no data exists that could.

## Decision — the monitoring contract

Deliberately the smallest contract the brief requires. Week 8 asks for a
**spec**; it does not ask for a scheduler, a dashboard or an alerting platform,
and none are specified here.

### 1. Denial-reason accuracy

**1.1 Authoritative source, in order.** The reason attached to a decision is,
in precedence order:

1. the licensed vendor's `reason_codes`, used verbatim; else
2. the deterministic stub's `_reason_codes`, permitted **only** where
   `ALLOW_MODEL_STUB` is set (dev/test), and always recorded against a
   `-stub`-suffixed `model_version`.

No third source is permitted. In particular, this service must never author a
consumer explanation of its own for a decision the model drove.

**1.2 Fail-closed behaviour, already implemented and hereby fixed as contract.**

| Vendor returns | Required behaviour |
|---|---|
| no `reason_codes` key | refuse the decision |
| `reason_codes: []` | refuse the decision |
| `reason_codes` not a list of strings | refuse the decision |
| an unrecognised reason string | see 1.4 |

A denial persisted without a reason, or with a reason the model did not
produce, is the Reg B defect this spec exists to prevent. Refusing is correct
even though it is less available.

**1.3 Distinct-reason measurement.** A reporting surface MUST be able to answer,
over a stated window and **grouped by `model_version`**:

- the count of *distinct* adverse-action reasons observed;
- the frequency of each reason;
- the count of decisions carrying no reason (which should be zero, and is a
  defect signal if not).

Grouping by `model_version` is required, not optional: a reason distribution
that silently mixes two model versions describes neither. `decision_events`
already carries every field this needs; no schema change is required.

**1.4 Unknown reasons.** A vendor reason string that this repository has no
approved consumer wording for MUST NOT be paraphrased, guessed at, or mapped to
the nearest known reason. Until an authoritative mapping exists (see
*Blocked*), the permitted behaviours are: refuse the decision, or surface the
vendor string unchanged and record it as unmapped. Choosing between those two is
part of the follow-up work, not of this spec.

**1.5 Fixtures only.** Acceptance MUST NOT require a live model call. The brief's
quota note is explicit, and reason-mapping logic is deterministic and testable
against fixtures.

### 2. Disparity check

**2.1 What the existing screen is.** Approval outcomes grouped by ZIP3;
approval rate per group; the four-fifths rule applied against the highest
group's rate; groups smaller than `min_group_size` (default 5) reported but not
flagged, because a rate over three applications is noise.

**2.2 What a flag means.** *Investigate.* It is not a finding of
discrimination, and it is not evidence about the model. ZIP3 is a geographic
proxy standing in for nothing that has been validated as a protected-class
proxy here.

**2.3 What this screen cannot do.** It measures the **final outcome**, which is
the product of the model, the policy thresholds, and every manual review in
between. It cannot attribute a disparity to the model, and therefore cannot
support or refute a claim about the model.

**2.4 Evidence status.** Local/training-only, against seeded fictional
applicants. No result from it may be presented as a production fair-lending
assessment.

### 3. What is required before anyone claims *this model is fair*

The brief asks what would have to be collected. This is that list, with the
current answer.

| Required evidence | Available on `main`? |
|---|---|
| Protected-class data (or a legally appropriate, validated proxy) | **No.** Only `applicants.zip_code` (`db/migrations/0014`), which is a geographic field, not a validated proxy |
| Model scores per decision | Yes — `decision_events.model_score` |
| Decisions/outcomes | Yes — `decision_events.decision` |
| Reason codes | Yes — `decision_events.reason_codes` |
| Model version alongside each of the above | Yes — `decision_events.model_version` |
| Feature attribution per decision | Partial — `top_features`, populated by the vendor; not populated for the stub |
| Sample size adequate for the groups compared | **Unknown.** Not measured; the ZIP3 screen's `min_group_size` is the only size guard anywhere |
| Vendor fairness documentation | **No.** None supplied with the licensed scorer |
| Longitudinal data across a stated window | Partially — `occurred_at` exists; no windowed reporting is built |

**Consequently, and this is the operative sentence of this spec: Meridian
cannot today make a fairness claim about this model, and MUST NOT make one.**
Not "has not yet"; *cannot*, because the first row of that table is missing and
nothing downstream substitutes for it.

**Protected-class attributes MUST NOT be manufactured, inferred, or synthesised
to close this gap.** A synthetic protected class produces a fairness result
about the synthesis, and presenting that as evidence would be worse than having
none.

## Non-goals

- A model rebuild, retrain, or threshold change.
- A marketing page describing the underwriting as advanced.
- A scheduled monitoring platform, dashboard, or alert. The brief asks for a
  spec; a scheduler is a separate decision with its own cost.
- Alert thresholds. What counts as too few distinct reasons, or an acceptable
  disparity ratio beyond the four-fifths screen already implemented, is a
  compliance judgement this repository has no authority to set.
- Inventing vendor reason codes. See *Blocked*.
- Widening the scorer to more products.

## Acceptance criteria

1. A reporting surface answers 1.3 over a stated window, grouped by
   `model_version`, from `decision_events` alone.
2. Reason-mapping behaviour in 1.1–1.4 is covered by fixture tests, with no live
   model call.
3. The ZIP3 screen's documentation states 2.2–2.4 wherever its output is
   presented.
4. `docs/model_card.md` and this spec do not contradict one another on what
   fairness evidence exists.
5. No document in this repository asserts that the model is fair.

## Failure behaviour

- Missing/empty/malformed vendor reason → the decision is refused, not
  defaulted.
- Reason reporting over a window containing more than one `model_version` →
  reported per version, never merged.
- ZIP3 groups below `min_group_size` → reported, not flagged.
- No ZIP data at all → an empty report, not a zero-disparity report. "No
  evidence of disparity" and "no evidence" are different statements and must not
  render identically.

## Security and privacy

- Reason reporting is aggregate. It MUST NOT expose applicant identifiers, and
  the disparity route is already staff-only.
- ZIP3 is a truncation of ZIP, retained because the four-fifths screen needs a
  grouping; full ZIP is not required for it and should not be surfaced by these
  reports.
- These reports describe applicants. Nothing in them belongs in an external
  trace or a third-party observability tool -- aggregate counts may be exported,
  the rows behind them may not.

## Governance

- **Owner:** Decisioning domain, as for `docs/model_card.md`.
- **Model-change review trigger:** any change to `AI_MODEL_VERSION`, the
  approve/refer/deny thresholds, or the scoring payload — the model card's
  existing trigger, extended to require re-running the reason-distribution
  measurement, because a new version can change the reason mix without changing
  any code here.
- **Reason-mapping-change review trigger:** any change to the reason constants
  or to `_reason_codes`, which requires updating the fixtures in the same
  change.
- **Model-card update trigger:** whenever a row in the *what is required before
  a fairness claim* table above changes state.
- **What blocks a wider rollout:** the brief's question was "wider is better,
  right?" The answer this spec supports: **rolling the scorer to further
  products is blocked while the fairness-evidence table has "No" in the
  protected-class row.** Widening multiplies decisions made by a model whose
  fairness cannot be evaluated. This is a documented engineering position, not
  an approval decision — approval authority is not defined in this repository
  and is not invented here.

## Blocked, and by whom

- **Vendor reason taxonomy — VENDOR-BLOCKED.** The set of reason codes the
  licensed scorer can emit is not documented anywhere in this repository, and no
  vendor contract or sample response is committed. A consumer-facing mapping
  cannot be written without it, and inventing categories (`HIGH_DTI`,
  `DEROGATORY_HISTORY`) would fabricate semantics the model may not have.
- **Protected-class data — CLIENT-BLOCKED.** Whether Meridian collects it,
  may collect it, or has an approved proxy is a compliance decision.
- **Disparity thresholds beyond four-fifths — CLIENT-BLOCKED.**

Related but separately deferred, and not reopened here: `docs/DEBT.md` **RF-25**
(whether staff may apply DTI manually in a referred review) and
`adr/0007-underwriting-policy-dti-fraud-gap.md` (G-DTI, why the system computes
no DTI and therefore cannot offer it as a reason).

## Evidence and test strategy

- Fixture tests for every branch of 1.2, already present in
  `services/decision-service/tests/test_decision.py` and extended by the
  follow-up work for 1.3–1.4.
- `db/tests/test_fair_lending_monitoring_spec.py` guards the claims this
  document makes about itself: that it names both halves of the contract, that
  it does not claim ZIP3 proves model fairness, that it identifies the missing
  data, and that it does not contradict the model card.
- `db/tests/test_docs_citations_resolve.py` already enforces that every path
  cited here resolves.

## Consequences

- The honest answer to the board is available and unflattering: the model emits
  two distinct reasons today, and its fairness cannot be assessed at all. Both
  are now written down where a regulator's question would find them.
- Two follow-ups are unblocked by this spec and one is not. The
  distinct-reason/frequency measurement (1.3) can be built now against
  `decision_events`. The ZIP3 documentation (2.2–2.4) can be corrected now. The
  consumer-facing reason mapping cannot, until the vendor taxonomy exists.
