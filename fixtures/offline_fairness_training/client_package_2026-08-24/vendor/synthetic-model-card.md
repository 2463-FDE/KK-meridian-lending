# Synthetic model card — CCUS-SYN-2026.08.24 (training only)

**Status:** Local/training-only synthetic card. Not vendor-issued. Not an approval.  
**Model:** Cedarline Consumer Unsecured Score (fictional)  
**Version:** CCUS-SYN-2026.08.24  
**Effective date:** 2026-08-24  
**Approval status:** Unapproved training fixture. Replace with the real vendor card before any real use.

## Intended use

Score a synthetic Meridian consumer-unsecured loan application and emit **specific principal reason codes** that map one-to-one to approved consumer wording. The card describes how this **training fixture** is meant to be used. It does not describe a live licensed scorer.

## Exclusions

- Real applicants, real bureaus, real payments, or production traffic.
- Mortgage, auto, or other products not in this fixture.
- Protected-class attributes as inputs, outputs, or runtime features.
- ZIP, ZIP3, or other geographic fields as fairness or scoring features.
- Generic score-only denials without specific mapped reasons.
- Any fairness or production-validation claim.

## Data provenance class

| Class | What it is | What it is not |
|---|---|---|
| Synthetic application fields | Requested amount, term, documented income, employment length/regularity | Real borrower data |
| Synthetic consumer-report categories | Delinquency, file thickness, inquiries, collections, bankruptcy, no-file | A real bureau extract |
| Isolated offline fairness fixture | Audit-only labeled rows in `fixtures/synthetic-offline-fairness-evaluation.csv` | A training, scoring, or runtime input |

No real vendor data, no real protected-class collection, and no learner or payment records are in this card.

## Performance and validation limitations

- No real vendor or model was tested. Figures in `synthetic-validation-summary.md` are synthetic acceptance outcomes only.
- Stability, drift, and reason-code fidelity statements in that summary are fixture narrative, not measured production performance.
- Score bands (approve ≥ 660, refer 600–659, deny < 600) are copied as **training-consistency labels** with Meridian's published local/training card. They are not independently validated here.
- Feature attribution is not supplied. Recording invented drivers would be fabricated evidence.

## Known risks

- Using this card as if it were vendor-issued.
- Mapping an unmapped or generic code to “the nearest” approved sentence.
- Placing fairness-fixture labels into scoring or traces.
- Treating a synthetic fairness summary as real-world validation.
- Generating consumer notices from raw codes.
- Ignoring a version conflict or stale document.

## Human-review and change trigger

Any change to version, score bands, taxonomy, or approved wording requires a new version/effective date and human review. Unmapped vendor codes fail closed. This card is superseded the moment a real vendor-issued current card is accepted.
