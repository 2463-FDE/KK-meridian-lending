# Offline fairness evaluation fixture — SYNTHETIC, TRAINING ONLY

**Status: `CLIENT-PACKAGE-RECEIVED-2026-08-24`.** The client's synthetic training
package arrived as an email attachment and is ingested under
[`client_package_2026-08-24/`](client_package_2026-08-24/), byte-for-byte, with
its own checksums verifying.

*This file previously read `CLIENT-PROVIDED-FIXTURE-NOT-PRESENT`, and that was
correct until 2026-08-24. It is recorded rather than erased because the interval
in which nothing had been supplied is why no evaluator was written earlier.*

**This file is repository-authored. Everything inside `client_package_2026-08-24/`
is client-authored and unmodified.** That boundary is the point of the directory
split: authorship is readable from location alone, and the client's
`SHA256SUMS.txt` covers exactly their files and none of ours.

- **SYNTHETIC** — every label in the package is fabricated training data.
- **TRAINING ONLY** — it supports an offline evaluation exercise and nothing else.
- **NOT VENDOR ISSUED** — no vendor produced it, and it is not vendor documentation.
- **NOT PRODUCTION EVIDENCE** — it is not fairness evidence, validation evidence,
  legal advice, or an implementation design.

## Provenance

| | |
|---|---|
| Original filename | `Kalabe-Synthetic-Vendor-Governance-Client-Inputs-Only-2026-08-24.zip` |
| Source | Client email attachment |
| Package version | `CCUS-SYN-2026.08.24` |
| Package effective date | 2026-08-24 |
| Ingested | 2026-08-27 |
| Files | 35 (34 checksummed + `SHA256SUMS.txt`) |
| Checksums at ingestion | **34/34 verified** |

The original `.zip` is deliberately **not committed**. It is the same bytes as the
extracted tree, and committing the attachment as well would store the payload
twice and put an email attachment into history. It is excluded via
`.git/info/exclude` rather than `.gitignore`, because keeping a local copy is one
person's working preference and not a rule for the repository.

`db/tests/test_client_package_is_byte_preserved.py` re-verifies every checksum
against the working tree on each run, so this table is a claim with a test behind
it rather than a note about something that was true once.

**`git diff --check` reports trailing whitespace in this package, and that is
correct and must not be fixed.** The client wrote their markdown with two-space
line breaks. Stripping them would change the bytes and fail all 34 checksums, so
the warning is the price of provenance rather than a defect. CI does not enforce
the check; repository-authored files here are whitespace-clean.

### Why `.gitattributes` mentions this directory

`core.autocrlf=true` is the Windows default and rewrites line endings on checkout.
Measured, not assumed: without the `-text` rule, a Windows checkout of this
directory fails **all 34** checksums — and fails them silently, because nothing
else in the repository reads these files byte-for-byte. The rule is what makes the
provenance claim survive a clone.

## Authority

Client decision, **2026-08-24**, now accompanied by the package it referred to:

> You do not have permission to collect real protected-class data for this
> demonstration. There is NO approved proxy. Do not create one, including from
> ZIP, ZIP3 or similar fields. Synthetic protected-class labels may be used ONLY
> in the isolated OFFLINE evaluation fixture included in the attached training
> package.

That decision superseded Week 8's ZIP3 outcome screen, which has been retired —
see [`specs/0003-fair-lending-monitoring.md`](../../specs/0003-fair-lending-monitoring.md)
§ *Superseded* and `db/tests/test_no_runtime_protected_class_proxy.py`.

## What the package contains

| Area | Files |
|---|---|
| `vendor/` | Synthetic vendor profile, 12-code reason taxonomy (CSV + JSON), approved consumer wording (MD + JSON), model card, validation summary, fairness summary |
| `policies/` | Fairness-data policy, adverse-action and reason-code boundary, vendor-document precedence and versioning |
| `fixtures/` | `synthetic-offline-fairness-evaluation.csv` — 32 audit-only rows |
| `evaluations/` | 28 acceptance cases, 14 negative fixtures |
| `sources/` | Regulatory notes and a source ledger |

## What it authorises, and what it does not

**Authorises** the isolated offline evaluation that Week 8 recorded as blocked.
It was blocked because there was nothing permitted to evaluate; there now is.

**Does not authorise** anything at runtime. Per the client's own precedence
policy this packet is the *lowest* tier — below current law and below
vendor-issued approved documents, of which **none are identified**. Real approved
vendor materials must replace this packet before any non-training use.

In particular, the twelve `CCUS-*` codes are **not** wired into
`services/decision-service/app/decision.py`. The local stub scorer does not emit
them, so mapping its two internal reasons onto this taxonomy would be
nearest-match substitution — which the client's adverse-action boundary prohibits
by name. `APPROVED_CONSUMER_REASONS` stays as it was.

## The tools that read it

All three live under `db/tools/`, deliberately outside `services/`, because
`test_no_runtime_code_reads_the_offline_fixture_location` forbids any runtime
module from so much as naming this directory.

| Tool | What it does |
|---|---|
| `db/tools/offline_fairness_eval.py` | Aggregate counts and outcome rates by each synthetic label column. **No verdict.** |
| `db/tools/governance_acceptance.py` | Executes the client's 28 acceptance cases against the mapping and refusal rules their policies describe — 28 resolved, 0 failed, nothing delegated |
| `db/tools/client_governance_package.py` | Loads the package, verifying checksums first; every other tool goes through it |

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

The client wrote acceptance cases for the first two — EVAL-16 rejects "the model
is fair based on the 32-row fixture", EVAL-15 rejects "this synthetic card is
production validated" — so these are not cautions this repository invented.

A real vendor response on a non-training path still fails closed while no approved
real taxonomy and consumer wording exist — see
`services/decision-service/app/decision.py::consumer_adverse_action_reason`.

## What arriving did *not* unblock

Searched literally, not inferred. The package contains no instruction on
payment-allocation placement, late-fee reassessment or compounding (D23), manual
DTI authority (RF-25), KYC/AML/UBO/sanctions (Week 9), record retention or
redaction (Week 10), or alert routing (D7). The only `debt_to_income` occurrences
are the placeholder `high_debt_to_income`, used as an *unknown code to be refused*
in their negative fixtures — which confirms this repository's existing position
rather than changing it. Every one of those items stayed exactly where it was
**as far as this package was concerned**, which is the only claim this paragraph
makes.

> **Not a current status list (added 2026-08-29).** Three of the items named
> above have since been decided, by separate client decisions that arrived
> outside this package: payment-allocation placement (both surfaces, built in
> #121), late-fee reassessment (`docs/DEBT.md` D23 — decided, not built) and
> manual DTI authority (RF-25 — decided, not built). The sentence above remains
> true of the package and is deliberately not rewritten; read it as "the package
> unblocked none of these", not as "none of these has been answered". The
> register entries are the current status.
