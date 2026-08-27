# Adverse-action and reason-code boundary (client)

**Version:** CCUS-SYN-2026.08.24  
**Effective date:** 2026-08-24  
**Not legal advice. Not a consumer notice.**

## Exact and specific reasons

A consumer-facing reason, when a demonstration scope actually issues one, must be:

- a **specific principal** reason;
- taken from a code the scorer actually emitted;
- mapped **one-to-one** through the approved wording table;
- accurate as a description of the factor considered or scored.

Current 12 CFR 1002.9 requires a statement of specific reasons. Statements that the action was based on internal standards or policies, or that the applicant failed to achieve a qualifying score, are insufficient. Official interpretations require the disclosed reasons to relate to and accurately describe the factors actually considered or scored.

## Prohibited

- Invented reasons.
- Post-hoc reasons written after the outcome to “sound better.”
- Generic, opaque, or score-only reasons.
- Discriminatory or proxy reasons.
- Passing an unmapped machine token through to the applicant.
- Nearest-match substitution.
- Generating a consumer notice unless the demonstration scope **explicitly** calls for one.

## Two artefacts

| Artefact | Where it may appear |
|---|---|
| Model reason evidence (raw code) | Audit / governance only |
| Approved consumer wording | Consumer-facing reason only after mapping |

Mapping must not erase the raw code in audit evidence.

## Human / compliance review

Unsupported cases — missing, unknown, generic, unmapped, conflicting, hostile vendor text, unauthorized role, or vendor failure — **refuse** the consumer-facing reason and escalate. Reviewers do not author a substitute reason for a model-driven denial.

## Roles

Borrowers do not approve mappings or see the fairness fixture. Staff (csr, underwriter, admin) may review governance outcomes. Mapping or policy changes require the designated compliance/staff review, not a borrower session and not an automated pass-through.
