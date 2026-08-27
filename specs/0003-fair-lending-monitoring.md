# Spec 0003 — Fair-lending monitoring: denial-reason accuracy and disparity

- **Status:** Accepted, and **AMENDED BY CLIENT DECISION 2026-08-24** — see
  *Superseding authority* below. §2's ZIP3 disparity screen is **SUPERSEDED —
  NOT CURRENT POLICY** and its implementation is retired. The rest of the
  document — §1's denial-reason contract, §4-§7 — stands unchanged.
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

## Superseding authority — client decision, 2026-08-24

Quoted, because the wording is the authority:

> You do not have permission to collect real protected-class data for this
> demonstration. There is NO approved proxy. Do not create one, including from
> ZIP, ZIP3 or similar fields. Synthetic protected-class labels may be used ONLY
> in the isolated OFFLINE evaluation fixture included in the attached training
> package. They must NEVER enter model inputs, runtime application inputs,
> application decisions, operational records, runtime database records, traces,
> telemetry, or consumer output.

**What that changes in this document.**

| | Previous authority (Week 8 brief) | Current authority (2026-08-24) |
|---|---|---|
| Runtime fairness evaluation | a ZIP3 outcome screen was permitted and built | **NOT PERMITTED** with protected-class data or an inferred proxy — including ZIP and ZIP3 |
| Offline evaluation | not contemplated | **PERMITTED**, against the single isolated synthetic fixture only |
| Protected-class collection | absent, and named as the blocker | **PROHIBITED** for this demonstration |
| Fairness claim | not supportable | **PROHIBITED**, and unchanged in substance |

**What was retired to comply**, on the day the decision arrived:
`services/origination-service/app/fair_lending.py` (deleted, not on `main`), its
route `GET /applications/fair-lending/zip-analysis` (no longer registered), and
`origination-service/tests/test_fair_lending.py` (deleted, not on `main`).
`applicants.zip_code` stays as
a postal-address component and is no longer fairness evidence.
`db/tests/test_no_runtime_protected_class_proxy.py` fails if the module, the
route, or a substitute proxy reappears — renaming ZIP3 would not make it
permitted, so the guard checks the shape as well as the name.

**Not affected:** §1's denial-reason-accuracy contract, the mapping seam, and the
per-`model_version` reason distribution. None of them touches a protected class
or a proxy; the reason distribution groups by model version.

**The client's confirmation request, answered in the repository:** protected-class
labels are confined to `fixtures/offline_fairness_training/` — supplied
2026-08-24 and ingested under `client_package_2026-08-24/`, `docs/DEBT.md`
**D24** — and real, currently approved vendor material must replace the synthetic
package before any non-training use. Arrival changes where the labels live, not
what may be claimed from them: the package is the lowest tier in the client's own
precedence policy, and no vendor-issued approved document is identified.

## Context — what exists on `main` today

The client's brief describes a system that no longer exists in three respects,
and saying so is part of the spec rather than a footnote: `GENERIC_REASONS` was
removed in Week 3, a model card exists, and `decision_events` already records
the model version and score behind every decision — plus `top_features`, but
**only** where the deterministic stub produced the score; a real vendor response
carries no attribution and is recorded as `null` (§3). The attached decision logs
are stale. What follows is measured against the code, not the brief's
description of it.

| Concern | Where it lives | State |
|---|---|---|
| Reason from the real vendor | `services/decision-service/app/decision.py` | vendor's `reason_codes` retained **verbatim as model evidence**; missing, empty, or non-list fails closed. A vendor code becomes consumer wording only through the approved mapping in `decision.py::consumer_adverse_action_reason` (§1.6, PR #66), and an unmapped code refuses the denial. *Before #66 no mapping layer stood between them and the consumer notice — that defect is Problem 3, kept as the record of what was fixed.* |
| Reason from the deterministic stub | `decision.py::_reason_codes` | derives from the larger of two shortfalls (bureau vs income); dev/test only, gated by `ALLOW_MODEL_STUB`, `model_version` suffixed `-stub` |
| Per-decision audit record | `decision_events` (`db/init/004_decision_events.sql`) | append-only by DB trigger; carries `model_version`, `model_score`, `top_features`, the inputs, `decision`, `reason_codes`, `occurred_at` |
| Outcome disparity | — | **Retired 2026-08-24 and does not exist on `main`.** Was a ZIP3 approval-rate four-fifths screen in `fair_lending.py` at `GET /applications/fair-lending/zip-analysis`; the client prohibited ZIP/ZIP3 as a protected-class proxy, so the module, the route and its tests are deleted. No runtime fairness evaluation replaces it |
| Aggregate reason monitoring over a window | `services/origination-service/app/reason_distribution.py` | distinct adverse-action reasons, each reason's frequency, and the no-reason count, per `model_version` over a stated window; staff-only `GET /applications/fair-lending/reason-distribution` (§1.3, PR #67). On demand — not scheduled, no threshold, no verdict |
| Approval-rate drift and score-distribution shift over time | — | not built; `docs/model_card.md` says so under *Monitoring*. Not asked for by the Week 8 brief — see *Non-goals* |

## Problem — as measured on the day this spec was accepted (2026-08-21)

**Status since acceptance, so this section is not read as current state.** The
mechanism for two of the three problems below was built the same day: the
mapping seam with its fail-closed default (Problem 3, PR #66) and
distinct-reason reporting (the second half of Problem 1, PR #67). Problem 2 is
unchanged and cannot be changed here — it is CLIENT-BLOCKED on protected-class
evidence, per §3. The three statements are left in the present tense of the day
they were written, because they are the record of what was wrong; current state
is the *State* column above, plus §1.3 and §1.6.

Two questions had no answer on that day, and they are different questions.

1. **Are the reasons accurate?** Reg B requires the specific principal reason.
   Nothing measures whether the reason a consumer receives corresponds to the
   driver that actually moved the decision, and nothing reports how many
   *distinct* reasons the system emits — the brief's first question, which before
   #67 was answerable only by reading source.
2. **Is the model fair?** The ZIP3 screen measures **outcomes**, not the model.
   It cannot support a statement about the model, and no data exists that could.
3. **Does a machine token reach the consumer?** Yes, by **two** paths, and the
   first version of this spec named only the second and less important one.

   - **`services/decision-service/app/graph.py::_node_finalize`** set
     `adverse_action_reason` to `reason_codes[0]` unchanged. That field travels
     to origination and is rendered to the applicant by
     `frontend/app/apply/page.tsx`. This is the applicant-facing path.
   - **`services/origination-service/app/decision_state.py::get_deny_reason`**
     returned `reason_codes[0]` unchanged into the 422 details on the boarding
     and offer-creation routes. Operational rather than applicant-facing, but
     still the model's own code repeated onward.

   The deterministic stub hides both, because its codes are full sentences; a
   real vendor returning `high_debt_to_income` would surface that token. A raw
   snake_case machine code is not a *specific reason* in the sense 12 CFR
   1002.9 requires. **Both paths are closed by the mapping seam (§1.6).**

## Decision — the monitoring contract

Deliberately the smallest contract the brief requires. Week 8 asks for a
**spec**; it does not ask for a scheduler, a dashboard or an alerting platform,
and none are specified here.

### 1. Denial-reason accuracy

**1.1 Two different artefacts, and conflating them is the defect.**

| | what it is | where it may appear |
|---|---|---|
| **Model reason evidence** | what the scorer reported — the vendor's `reason_codes` verbatim, or the deterministic stub's `_reason_codes` | `decision_events`, audit and governance surfaces |
| **Consumer adverse-action reason** | the specific reason a declined applicant is told | `adverse_action_reason`, notices, anything applicant-facing |

The first is authoritative about **the model**. It is *not* automatically
authoritative **wording**, and a vendor code becomes consumer text only by
passing through an approved mapping (1.6).

Model reason evidence comes from exactly two sources, in precedence order: the
licensed vendor's `reason_codes`; else the deterministic stub's
`_reason_codes`, permitted **only** where `ALLOW_MODEL_STUB` is set (dev/test)
and always recorded against a `-stub`-suffixed `model_version`. No third source
is permitted, and this service must never author a reason of its own for a
decision the model drove.

Provenance is preserved: mapping to consumer wording MUST NOT overwrite or
discard the raw code in `decision_events`. The audit record answers "what did
the model say"; the notice answers "what is the applicant told". Both are
needed and they are not the same field.

**1.2 Fail-closed behaviour, already implemented and hereby fixed as contract.**

| Vendor returns | Required behaviour |
|---|---|
| no `reason_codes` key | refuse the decision |
| `reason_codes: []` | refuse the decision |
| `reason_codes` not a list of strings | refuse the decision |
| a code with no approved consumer mapping | see 1.4 — **fail closed** |

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

**1.4 Unmapped vendor reasons fail closed.** A vendor reason code with no
approved consumer wording MUST NOT be paraphrased, guessed at, mapped to the
nearest known reason, replaced by a generic fallback, or **surfaced to the
consumer unchanged**. All five are ways of putting an unapproved statement in
front of a declined applicant, and the last is the one that looks harmless.

The required behaviour is a single one: **the decision fails closed before any
consumer-facing reason is produced.**

An earlier draft of this spec permitted "surface the vendor string unchanged
and record it as unmapped" as an alternative. That was wrong and is recorded
rather than silently deleted: it would have made the defect in Problem 3 the
*governed* behaviour, which is worse than the defect being an oversight.

Failing closed MUST be atomic with respect to the audit trail. An unmapped code
must not leave a committed `decisions` row carrying a machine token, nor a
`decision_events` row whose decision never completed. Whichever write happens
first, neither half may survive alone.

**1.6 The approved mapping.** Consumer wording is produced by an explicit,
deterministic mapping from model reason code to approved consumer sentence. Two
properties matter more than its shape:

- **the mechanism may be built now** — a mapping seam with a fail-closed
  default requires no vendor knowledge at all;
- **its real-vendor entries may not be invented.** The taxonomy is
  VENDOR-BLOCKED (see *Blocked*), and `high_debt_to_income` — which appears in
  this repository exactly twice, both times as a test author's placeholder —
  is **not** evidence of a vendor taxonomy entry and MUST NOT be promoted into
  the mapping.

The deterministic stub's two reasons are already approved consumer sentences,
because their meaning is owned by the stub logic in this repository rather than
by a third party. **Two reasons is not a defect** where two drivers is what the
stub genuinely has.

**1.5 Fixtures only.** Acceptance MUST NOT require a live model call. The brief's
quota note is explicit, and reason-mapping logic is deterministic and testable
against fixtures.

### 2. Disparity check — **SUPERSEDED — NOT CURRENT POLICY**

**Current contract, which replaces everything in this section:**

- **Runtime fairness evaluation: NOT PERMITTED.** Not with protected-class data,
  which this demonstration may not collect, and not with an inferred proxy, which
  it may not create — expressly including ZIP and ZIP3.
- **Offline synthetic evaluation: PERMITTED**, and only against the single
  isolated fixture at `fixtures/offline_fairness_training/`. Offline means a CLI
  or test package: it reads that directory, never the runtime tables, writes no
  label anywhere, calls no model, and emits aggregate output that says SYNTHETIC
  / TRAINING ONLY on its face.
- **No production fairness claim**, from either. Unchanged, and now doubly so.

> ### Superseded 2026-08-24 — the original §2, kept verbatim
>
> The four paragraphs below described a screen that was built, tested and
> shipped. They are the record of a decision the client has since reversed, and
> deleting them would hide that this repository once operated a geographic
> proxy — which is exactly the thing a reader should be able to see was stopped.
> Nothing below is current policy, and the code it describes no longer exists.
>
> **2.1 What the existing screen is.** Approval outcomes grouped by ZIP3;
> approval rate per group; the four-fifths rule applied against the highest
> group's rate; groups smaller than `min_group_size` (default 5) reported but not
> flagged, because a rate over three applications is noise.
>
> **2.2 What a flag means.** *Investigate.* It is not a finding of
> discrimination, and it is not evidence about the model. ZIP3 is a geographic
> proxy standing in for nothing that has been validated as a protected-class
> proxy here.
>
> **2.3 What this screen cannot do.** It measures the **final outcome**, which is
> the product of the model, the policy thresholds, and every manual review in
> between. It cannot attribute a disparity to the model, and therefore cannot
> support or refute a claim about the model.
>
> **2.4 Evidence status.** Local/training-only, against seeded fictional
> applicants. No result from it may be presented as a production fair-lending
> assessment.
>
> *Read 2.2 alongside the 2026-08-24 decision: the spec already said ZIP3 stood
> in for nothing validated. The client's answer to that was to prohibit the
> substitution rather than to validate it.*

### 3. What is required before anyone claims *this model is fair*

The brief asks what would have to be collected. This is that list, with the
current answer.

| Required evidence | Available on `main`? |
|---|---|
| Protected-class data (or a legally appropriate, validated proxy) | **PROHIBITED**, which is a stronger statement than unavailable. Client decision 2026-08-24: no real collection for this demonstration, no approved proxy, and none may be created from ZIP, ZIP3 or similar. `applicants.zip_code` remains a postal-address component. *This cell read "**No.** Only `applicants.zip_code` … not a validated proxy" until that decision* |
| Model scores per decision | Yes — `decision_events.model_score` |
| Decisions/outcomes | Yes — `decision_events.decision` |
| Reason codes | Yes — `decision_events.reason_codes` |
| Model version alongside each of the above | Yes — `decision_events.model_version` |
| Feature attribution per decision | Partial, and in the opposite direction to what a reader expects — `top_features` is populated **only for the deterministic stub**, whose score formula is known and reproducible, and is recorded as `null` for a real vendor response because `_ScorerResponse` carries no attribution. A locally-computed "explanation" for a vendor score would be fabricated audit data (`services/decision-service/tests/test_decision.py`) |
| Sample size adequate for the groups compared | **Unknown, and now unmeasurable at runtime.** Not measured. The ZIP3 screen's `min_group_size` was the only size guard anywhere, and that screen was deleted on 2026-08-24 (§2, superseded); no runtime fairness evaluation exists to size. *This cell cited `min_group_size` as a present guard until that deletion* |
| Vendor fairness documentation | **No.** None supplied with the licensed scorer |
| Longitudinal data across a stated window | Partially — `occurred_at` exists, and windowed **reason** reporting is built (§1.3, PR #67). Windowed reporting of approval rates or score distributions is not, and is a *Non-goal* here |

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
  disparity ratio, is a compliance judgement this repository has no
  authority to set. *This bullet read "beyond the four-fifths screen already
  implemented" until 2026-08-24; that screen is deleted (§2, superseded), so
  there is no implemented baseline to be beyond.*
- Inventing vendor reason codes. See *Blocked*.
- **Any runtime fairness evaluation**, and any proxy for a protected class —
  geographic, surname-based, census-based, BISG or otherwise. Prohibited by the
  client, not merely out of scope (2026-08-24).
- **Treating the client's synthetic training package as vendor documentation.**
  It is synthetic, training-only, and not vendor-issued. Real, currently approved
  vendor material must replace it before any non-training use, and a real vendor
  response on a non-training path still fails closed until then.
- Widening the scorer to more products.

## Acceptance criteria

1. A reporting surface answers 1.3 over a stated window, grouped by
   `model_version`, from `decision_events` alone.
2. Reason-mapping behaviour in 1.1–1.6 is covered by fixture tests, with no live
   model call.
3. A fixture proves that an **unmapped vendor reason code cannot reach
   `adverse_action_reason`** or any consumer-facing output — asserted on the
   output, not on the mapping table, because a table can be correct while a
   caller bypasses it.
4. A fixture proves the fail-closed path leaves **no partial committed state**:
   no `decisions` row carrying a raw machine token, and no orphaned
   `decision_events` row for a decision that never completed.
5. A fixture proves the raw vendor code is still **retained as model evidence**
   for decisions that do complete — the mapping must not destroy provenance.
6. **Withdrawn 2026-08-24, with the screen it was about.** It read: "The ZIP3
   screen's documentation states 2.2–2.4 wherever its output is presented."
   There is no output to state anything beside — the screen was deleted, not
   disabled (§2, superseded). Left numbered rather than renumbered, so a
   reference to "criterion 7" elsewhere still points at the same criterion.
7. `docs/model_card.md` and this spec do not contradict one another on what
   fairness evidence exists.
8. No document in this repository asserts that the model is fair.

## Failure behaviour

- Missing/empty/malformed vendor reason → the decision is refused, not
  defaulted.
- Vendor reason code with no approved consumer mapping → the decision is
  refused. Never the raw token, never a nearest match, never a generic string.
- A refusal on either of the two rows above → no partial committed state.
- Reason reporting over a window containing more than one `model_version` →
  reported per version, never merged.
- *Two ZIP3 failure modes were specified here and are withdrawn with the screen
  (§2, superseded 2026-08-24): groups below `min_group_size` reported rather than
  flagged, and no ZIP data producing an empty report rather than a zero-disparity
  one.* The distinction they turned on survives them and still binds anything
  built later: **"no evidence of disparity" and "no evidence" are different
  statements and must not render identically.**

## Security and privacy

- Reason reporting is aggregate. It MUST NOT expose applicant identifiers.
  *This bullet also said "the disparity route is already staff-only" until
  2026-08-24. That route is not staff-only — it is **not registered at all**,
  which `services/origination-service/tests/test_staff_gated_routes_require_internal_token.py::test_the_zip_analysis_route_is_gone_rather_than_gated`
  asserts by name. Gated and absent are different answers, and the weaker one
  was on the page.*
- `applicants.zip_code` is retained as a **postal-address component and nothing
  else**. *This bullet read "ZIP3 is a truncation of ZIP, retained because the
  four-fifths screen needs a grouping" until 2026-08-24; the client prohibited
  any protected-class proxy including one derived from ZIP or ZIP3, so no
  grouping needs it and none may be created.* Full ZIP is not required by these
  reports and must not be surfaced by them.
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
  vendor contract or sample response is committed. Inventing categories
  (`HIGH_DTI`, `DEROGATORY_HISTORY`) would fabricate semantics the model may not
  have.

  **This blocks the mapping's CONTENT, not its MECHANISM.** The two are
  separable and the distinction is the practical output of this spec: a mapping
  seam whose default is to fail closed can be built and tested today with an
  empty real-vendor table, and it removes the Problem 3 defect immediately.
  Entries get added when the client supplies the taxonomy and approved wording,
  and not before.
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
- Three follow-ups are unblocked by this spec and one is half-blocked. The
  distinct-reason/frequency measurement (1.3) can be built now against
  `decision_events`. *The ZIP3 documentation follow-up (2.2–2.4) is withdrawn: §2 is superseded and
the screen deleted, so there is nothing left to correct.* The
  mapping **mechanism** (1.6) can be built now, with a fail-closed default and
  no real-vendor entries — which is what actually closes Problem 3. Only the
  mapping's real-vendor **entries** wait on the taxonomy.
- Until that mechanism lands, a real vendor deployment would put machine tokens
  in front of declined applicants. The deterministic stub conceals it, so this
  is written down where the next person will find it.
