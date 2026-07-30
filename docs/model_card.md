# Model Card — Licensed AI Credit Scorer

**Status:** first version, written to close a real gap: nothing in this repo
documented which model, version, or inputs drive a lending decision before
this. See ROADMAP.md Week 8 finding — "no model card, no record of which
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
persisted to the append-only `decision_events` table before a result is
returned.

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
| `bureau_score` | Credit bureau pull (Experian, or its dev stub) — the step immediately before this one |
| `requested_amount` | Applicant-submitted |
| `term_months` | Applicant-submitted |
| `income` | Applicant-submitted |

The model is not told the applicant's name, SSN, address, or ZIP — none of
those fields are in the payload built in `_call_ai_scorer()`.

## Output / audit record

Every run persists to `decision_events`:

- `model_score`, `model_version`, `bureau_score`, `requested_amount`,
  `term_months`, `annual_income`
- `reason_codes` — for a real vendor response, these come **from the vendor
  itself**, never re-derived locally (a locally-guessed reason could name a
  driver — e.g. bureau vs. income — that isn't actually what the licensed
  model weighed, since it also sees `requested_amount`/`term_months`)
- `top_features` — **only populated for the dev/test stub**, whose score
  formula is known and reproducible. For a real vendor response this is
  recorded as `null`: `_ScorerResponse` doesn't include feature attribution,
  so persisting a locally-computed "explanation" here would be fabricated
  audit data, not what actually drove the score. Recording `null` is more
  honest than guessing.

## Known limitations (open, not yet closed)

- **No fairness/disparate-impact testing has ever been run against this
  model.** Not because it passed one — because the schema doesn't have the
  field a geography-based fairness check would need. See "ZIP field" below.
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
  is traced via LangSmith (project `2463-fde`) — bureau pull, scoring call,
  and persistence are each individually visible.
- `decision_events` itself is the long-term audit record — append-only,
  DB-trigger-enforced, queryable for any past decision.
- **Not yet built:** aggregate monitoring of the model's own behavior over
  time (approval-rate drift, score distribution shift, reason-code
  frequency). Today this data can be queried per-decision but isn't
  dashboarded anywhere.

## Owner

Decisioning domain. Update this card whenever `AI_MODEL_VERSION` changes, the
approve/refer/deny thresholds change, or a new input is added to the scoring
payload.
