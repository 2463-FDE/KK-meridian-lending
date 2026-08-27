# Synthetic fairness summary — offline fixture only (training)

**Status:** Offline synthetic analysis for client acceptance practice. **Not a real-world fairness result. Not a production claim.**  
**Version:** CCUS-SYN-2026.08.24  
**Effective date:** 2026-08-24  
**Population:** `fixtures/synthetic-offline-fairness-evaluation.csv` only (32 synthetic rows).

## What was examined (acceptance outcome, not a method design)

On the isolated offline fixture only, the packet requires the reviewer to confirm:

- labels are synthetic, audit-only, and prefixed or captioned as such;
- rows are balanced across the synthetic sex, race/ethnicity stand-in, and age-band columns;
- no real identifiers, ZIP, payment data, or names appear;
- approve / refer / deny labels are mixed rather than a single outcome per group;
- this file is referenced by evaluations as **excluded** from runtime and vendor inputs.

No disparity ratio, four-fifths result, or model-level adverse-impact number is reported here. Computing one from 32 synthetic rows and presenting it as evidence would be a fairness overclaim.

## Metrics named as acceptance outcomes (not tooling)

| Acceptance outcome | Pass condition |
|---|---|
| Isolation | Protected-class columns exist only in the fairness fixture |
| Balance | Each synthetic sex, race stand-in, and age band appears equally often |
| Non-identification | No SSN, name, address, ZIP, phone, email, or real account id |
| Non-claim | Packet text never states the model is fair or production-validated |
| Non-proxy | ZIP, ZIP3, and other geographic fields are absent from this fixture and are not treated as protected-class stand-ins |

This summary does not select libraries, thresholds, training procedures, or monitoring design.

## Limitations

- 32 rows cannot support a statistical fairness conclusion.
- Synthetic labels describe the fixture, not people.
- Meridian currently has no approved protected-class collection for this demonstration. This fixture does not fill that gap.
- Outcome labels in the fixture are not model-output proof.

## Required refusal

Any statement that “the model is fair,” “this validates production,” or “ZIP3 shows the model is unbiased” is an **unsupported vendor or client claim** and must be refused. Escalate; do not soften into a nearest acceptable sentence.
