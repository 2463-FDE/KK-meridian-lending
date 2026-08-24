# Offline fairness evaluation fixture — SYNTHETIC, TRAINING ONLY

**Status: `CLIENT-PROVIDED-FIXTURE-NOT-PRESENT`.** Nothing has been supplied yet,
and nothing in this directory is authored by this repository. The location, its
rules and its labels exist first so that when the package arrives there is
already exactly one place it may live.

- **SYNTHETIC** — every label here is fabricated training data.
- **TRAINING ONLY** — it supports an offline evaluation exercise and nothing else.
- **NOT VENDOR ISSUED** — no vendor produced it, and it is not vendor documentation.
- **NOT PRODUCTION EVIDENCE** — it is not fairness evidence, validation evidence,
  legal advice, or an implementation design.

## Authority

Client decision, **2026-08-24**:

> You do not have permission to collect real protected-class data for this
> demonstration. There is NO approved proxy. Do not create one, including from
> ZIP, ZIP3 or similar fields. Synthetic protected-class labels may be used ONLY
> in the isolated OFFLINE evaluation fixture included in the attached training
> package.

That decision superseded Week 8's ZIP3 outcome screen, which has been retired —
see [`specs/0003-fair-lending-monitoring.md`](../../specs/0003-fair-lending-monitoring.md)
§ *Superseded* and `db/tests/test_no_runtime_protected_class_proxy.py`.

## What must be supplied

The client's training package. Per the client's own description it contains a
synthetic reason-code taxonomy, approved training consumer wording, a synthetic
model card, a validation summary, a fairness summary, client policies and
acceptance tests.

**It is not reconstructed here.** A fixture invented in this repository would be
the same defect the client warned about — synthetic material presented as
authority — arriving one step earlier. Until the package is supplied:

- no offline evaluator is written, because there is nothing for it to read;
- no synthetic taxonomy is committed, because inventing one would create a
  reason-code vocabulary nobody approved;
- `docs/DEBT.md` carries the dependency so it is tracked rather than remembered.

## Rules for anything that lands here

Binding on the fixture and on any evaluator written against it.

1. **Offline only.** The evaluator is a CLI or test package, never a FastAPI
   route. There is no runtime path to this directory, and
   `test_no_runtime_code_reads_the_offline_fixture_location` fails if a service
   module so much as names it.
2. **Reads this directory and nothing else.** It never queries `applicants`,
   `applications`, `decisions` or `decision_events`.
3. **Writes no label anywhere.** Not to PostgreSQL, not to a log, not to a trace,
   not to telemetry, not to a model request, not to consumer output.
4. **Calls no model and no vendor.** The package authorises no live call.
5. **Aggregate output only.** No per-record label leaves the evaluation.
6. **Says what it is.** Any output states SYNTHETIC / TRAINING ONLY on its face.

## What may never be claimed from it

- that the model is fair;
- that it is production validated;
- that it is approved for real consumer decisions;
- that vendor governance documentation exists.

Real, currently approved vendor material must replace this package before any
non-training use. A real vendor response on a non-training path still fails
closed while no approved real taxonomy and consumer wording exist — see
`services/decision-service/app/decision.py::consumer_adverse_action_reason`.
