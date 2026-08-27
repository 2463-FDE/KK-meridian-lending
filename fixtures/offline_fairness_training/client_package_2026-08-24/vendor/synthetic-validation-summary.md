# Synthetic validation summary — CCUS-SYN-2026.08.24 (training only)

**Status:** Synthetic acceptance narrative. **No real vendor, model, or data was tested.**  
**Version:** CCUS-SYN-2026.08.24  
**Effective date:** 2026-08-24  
**Test population:** 32 wholly synthetic, non-identifying fixture rows used only for offline governance checks. Not a production population. Not a statistically adequate sample for any real claim.

## Scope

This summary records **client acceptance outcomes** for the training packet:

- every taxonomy code has exactly one approved-wording id;
- generic / opaque / “qualifying score” / “internal policy” wording is refused;
- missing or unknown codes refuse the decision and leave no consumer wording;
- version and stale-document conflicts stop and escalate;
- the offline fairness fixture is excluded from every vendor/model/runtime input.

It is not a lab report, not a vendor validation, and not a production authorization.

## Performance and stability limitations

| Topic | Fixture statement | Limitation |
|---|---|---|
| Score band application | Deny / refer / approve bands are applied as labels on synthetic rows | Not measured against a live scorer |
| Reason-code coverage | Twelve codes; each deny row in the acceptance set carries a mapped code | Does not prove a real vendor emits only these codes |
| Stability | No multi-window production series exists | Drift monitoring is **not** implemented by this packet and is not designed here |
| Sample size | 32 synthetic rows | Inadequate for any real performance or fairness claim |

## Reason-code fidelity (acceptance outcome)

Required outcome for this packet:

1. Code → wording id is one-to-one and stable.
2. Consumer wording matches the factor the code names.
3. Audit evidence retains the raw code; mapping must not erase it.
4. A code outside the taxonomy, or a generic phrase, never becomes consumer wording.

These are pass/fail acceptance rules in `evaluations/`, not a measured fidelity rate from a live model.

## Drift and governance outcomes

- A newer vendor version without a matching approved taxonomy/wording set is a **stop**.
- Mixing two model versions in one reason-distribution claim is a **stop**.
- This packet does not specify thresholds, schedulers, or monitoring design.

## Explicit non-test statement

**No real vendor was called. No real model was scored. No real applicant or payment data was used.** Synthetic results must not be represented as production or real-world validation.
