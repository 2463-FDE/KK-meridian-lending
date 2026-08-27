# Governance acceptance evaluations (client outcomes)

**Version:** CCUS-SYN-2026.08.24
**Effective date:** 2026-08-24
**Count:** 28 client acceptance cases in `governance-acceptance-evaluations.jsonl`.

These cases record required **inputs, outcomes, refusals, escalations, and pass criteria**. They are not an implementation design and contain no implementation strategy field.

## Coverage

| Category | Eval IDs |
|---|---|
| Taxonomy / wording alignment | EVAL-01 to EVAL-06 |
| Missing or unknown reason | EVAL-07, EVAL-08 |
| Generic reason refusal | EVAL-09, EVAL-10 |
| Synthetic-label isolation | EVAL-11, EVAL-28 |
| Proxy prohibition | EVAL-12, EVAL-27 |
| Version conflicts | EVAL-13 |
| Stale vendor docs | EVAL-14 |
| Unsupported vendor claim | EVAL-15 |
| Fairness overclaim | EVAL-16 |
| Adverse-action specificity | EVAL-17, EVAL-18, EVAL-26 |
| Unauthorized role | EVAL-19, EVAL-20 |
| Sensitive-data retention | EVAL-21 |
| Prompt injection (hostile vendor text) | EVAL-22 |
| Vendor or model failure | EVAL-23, EVAL-24 |
| Human escalation | EVAL-25 |

## Negative fixtures

Files under `evaluations/fixtures/` are **never approved inputs**. They exist so an acceptance review can prove refusal. Do not copy them into vendor/, fixtures/ (other than the isolated fairness file), or runtime.

Each evaluation that names a `negative_fixture` path must resolve to a file in that folder.
