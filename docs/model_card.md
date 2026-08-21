# Model Card — Licensed AI Credit Scorer

**Status:** Local/training-only. First version, written to close a real gap: nothing in this repo
documented which model, version, or inputs drive a lending decision before
this. See [ROADMAP.md](ROADMAP.md) Week 8 finding — "no model card, no record of which
features/model version produced any given decision, no fairness testing ever
performed."

## What this model does

Scores a loan application (0–1000) as part of the decisioning chain in
`decision-service` (`app/decision.py`, `app/graph.py`). The score maps to an
outcome:

| Score | Outcome |
|---|---|
| ≥ 660 | Approve |
| 600–659 | Refer (manual review) |
| < 600 | Deny |

This is one step in a larger pipeline, not a standalone decision: a credit
bureau pull happens first, and every run — inputs, score, outcome — is
recorded in the append-only `decision_events` table. Since PR #6 the scorer
itself persists nothing: `decision-service` computes and returns, and
`origination-service` writes the `decisions` row and the `decision_events`
row together in one transaction after re-checking that the application is
still decidable. A computed score that loses that race is discarded rather
than audited, so `decision_events` contains only scores that actually became
decisions.

## Vendor / version

- **Provider:** `creditai-2026.1` (`AI_MODEL_VERSION`, `AI_MODEL_BASE_URL` in
  `app/config.py`) — a licensed third-party scoring model, not built in-house.
- **Dev/test stub:** when `AI_MODEL_API_KEY` is unset, a deterministic local
  formula stands in (`_stub_model_score`), and its `model_version` is always
  suffixed `-stub` so a stub score can never be mistaken for a real vendor
  response in the audit trail.
- **Fails closed outside dev/test:** if the real vendor is unreachable or
  misconfigured, the request fails (`ModelUnavailableError`) rather than
  silently falling back to the stub score.

## Inputs

| Field | Source |
|---|---|
| `bureau_score` | Credit bureau pull via the `BureauClient` seam (`app/bureau.py`) — an HTTP client for a real provider, or `StubBureauClient` in dev/test. No real bureau integration has ever been exercised; the idempotency-key contract is verified against our own stub only. |
| `requested_amount` | Applicant-submitted |
| `term_months` | Applicant-submitted |
| `income` | Applicant-submitted |

The model is not told the applicant's name, SSN, address, or ZIP — none of
those fields are in the payload built in `_call_ai_scorer()`.

## Output / audit record

Every run that is accepted as a decision is written to `decision_events` (by
origination — see above):

- `model_score`, `model_version`, `bureau_score`, `requested_amount`,
  `term_months`, `annual_income`
- `reason_codes` — for a real vendor response, these come **from the vendor
  itself**, never re-derived locally (a locally-guessed reason could name a
  driver — e.g. bureau vs. income — that isn't actually what the licensed
  model weighed, since it also sees `requested_amount`/`term_months`).
  Retained here **verbatim, as model evidence**, and that is now a different
  artefact from what a declined applicant is told: the consumer-facing
  adverse-action reason comes from an approved mapping
  (`decision.py::APPROVED_CONSUMER_REASONS`, spec 0003 §1.6), and a code with
  no approved wording **refuses the decision** rather than being repeated to a
  person. Before that mapping existed, `reason_codes[0]` was published straight
  into `adverse_action_reason`; the deterministic stub concealed it because its
  codes are full sentences
- `top_features` — **only populated for the dev/test stub**, whose score
  formula is known and reproducible. For a real vendor response this is
  recorded as `null`: `_ScorerResponse` doesn't include feature attribution,
  so persisting a locally-computed "explanation" here would be fabricated
  audit data, not what actually drove the score. Recording `null` is more
  honest than guessing.

## Known limitations (open, not yet closed)

- **No fairness/disparate-impact testing has ever been run against this
  model.** Still true, and worth stating precisely, because a related control
  now exists and must not be mistaken for this one:
  - What EXISTS (PR #6, implemented and unit-tested): a **portfolio-level**
    ZIP3 four-fifths-rule screen over recorded *approval outcomes*
    (`origination-service/app/fair_lending.py`, staff-only
    `GET /applications/fair-lending/zip-analysis`,
    `tests/test_fair_lending.py`). `applicants.zip_code`
    (`db/migrations/0014`) is the field that made it possible.
  - What does NOT exist: any fairness evaluation of **the model itself** — no
    protected-class or proxy analysis of its scores, no disparate-impact
    testing of `creditai-2026.1`, no adverse-impact ratio computed on model
    output as distinct from final outcomes, and no vendor fairness
    documentation.
  - The screen is therefore an outcome monitor, **not** model validation, and
    it is local/training-only: it has never been run against production data,
    because no production environment exists. No compliance conclusion should
    be drawn from it.
- **No model documentation from the vendor is stored in this repo** beyond
  the version string — no card, no methodology, from `creditai-2026.1`
  itself. This card documents how *Meridian* uses the model, not how the
  vendor built it.
- **Reason-code quality depends on the vendor.** `_reason_codes()` (the local
  bureau/income shortfall formula) is only authoritative for the dev stub. A
  real vendor response with a sub-660 score and an empty `reason_codes` list
  fails closed rather than guessing — but that means a vendor bug or
  incomplete integration shows up as a hard failure for the applicant, not a
  degraded answer.

## Monitoring

- Every run of the decision graph (`app/graph.py`, a LangGraph `StateGraph`)
  is traced via LangSmith (project `2463-fde`) — bureau pull and scoring call
  are each individually visible. Persistence is no longer part of this graph;
  it happens in origination. Local/training-only: tracing is opt-in and has
  only ever run against seeded fictional applicants.
- `decision_events` itself is the long-term audit record — append-only,
  DB-trigger-enforced, queryable for any past decision.
- **Reason-code frequency is now measurable**, per model version and over a
  stated window: `GET /applications/fair-lending/reason-distribution`
  (`origination-service/app/reason_distribution.py`, spec 0003 §1.3). It
  reports the count of distinct adverse-action reasons, the frequency of each,
  and the count of denials carrying no reason. Staff-only, aggregate,
  on-demand. It sets **no threshold and reaches no verdict** — what counts as
  too few distinct reasons is a compliance judgement, and this repository has
  no authority to set one.
- **Still not built:** approval-rate drift and score-distribution shift over
  time. Both are queryable per-decision from `decision_events`; neither is
  reported or dashboarded anywhere, and neither is scheduled or alerted.

## Owner

Decisioning domain. Update this card whenever `AI_MODEL_VERSION` changes, the
approve/refer/deny thresholds change, or a new input is added to the scoring
payload.
