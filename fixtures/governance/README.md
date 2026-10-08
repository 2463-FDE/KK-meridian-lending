# Governance fixture — SYNTHETIC, TRAINING ONLY

Repository-authored synthetic data for Meridian's offline governance tooling.
Version **SYN-GOV-2.0**.

- **SYNTHETIC** — every reason code, sentence and label here is fabricated.
- **TRAINING ONLY** — it supports offline evaluation and nothing else.
- **NOT VENDOR ISSUED** — no vendor produced it, and it is not vendor documentation.
- **NOT PRODUCTION EVIDENCE** — it is not fairness evidence, validation evidence,
  legal advice or an implementation design.

## Contents

| Path | What it holds |
|---|---|
| `vendor/` | A synthetic 12-code reason-code taxonomy and the approved consumer wording each code maps to, one-to-one |
| `policies/` | Adverse-action and reason-code boundary, fairness-data policy, document precedence and versioning |
| `fixtures/synthetic-offline-fairness-evaluation.csv` | 32 audit-only rows carrying synthetic protected-class labels |
| `evaluations/` | 28 acceptance cases and the negative fixtures they name |
| `SHA256SUMS.txt` | Checksums of every other file here |

## The tools that read it

All three live under `db/tools/`, outside `services/`, because no runtime module
may name this directory (`test_no_runtime_code_reads_the_offline_fixture_location`).

| Tool | What it does |
|---|---|
| `db/tools/governance_fixture.py` | Loads the fixture after verifying `SHA256SUMS.txt`; fails closed on a changed, missing or unlisted file |
| `db/tools/governance_acceptance.py` | Resolves reason codes to approved wording or refuses, and runs all 28 acceptance cases |
| `db/tools/offline_fairness_eval.py` | Aggregate counts and outcome rates by each synthetic label column. **No verdict.** |

## Rules

1. **Offline only.** CLI or test, never a FastAPI route or job.
2. **Reads this directory and nothing else.** No `applicants`, `applications`,
   `decisions` or `decision_events`, and no database connection.
3. **Writes no label anywhere** — not to PostgreSQL, logs, traces, telemetry,
   model requests or consumer output.
4. **Calls no model and no vendor.**
5. **Aggregate output only.** No per-record label leaves the evaluation.
6. **Says what it is.** Every output carries SYNTHETIC / TRAINING ONLY.

The twelve `SYN-*` codes are not wired into
`services/decision-service/app/decision.py`. The local stub scorer does not emit
them, and mapping its internal reasons onto this taxonomy would be the
nearest-match substitution the boundary policy prohibits.

## What may never be claimed from it

- that the model is fair;
- that it is production validated;
- that it is approved for real consumer decisions;
- that vendor governance documentation exists.

EVAL-16 and EVAL-15 reject the first two claims directly.

Changing a file here means regenerating `SHA256SUMS.txt`; the integrity test
(`db/tests/test_governance_fixture_integrity.py`) fails otherwise.
