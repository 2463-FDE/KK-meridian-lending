# Vendor-document precedence and versioning (client)

**Version:** CCUS-SYN-2026.08.24  
**Effective date:** 2026-08-24

## Tiers (highest wins)

1. **Applicable current law and regulation** (for adverse-action content: current 12 CFR 1002.9 and official interpretations). This packet does not amend the law.
2. **Current vendor-issued approved documents** (taxonomy, wording, model card, validation) once they exist. None are identified today.
3. **Current client policy** in this `policies/` folder (and any later client-approved successor).
4. **This synthetic training packet.** Lowest tier. Replaceable. Never represented as vendor-issued.

## Version rule

Within a tier, the **current version and effective date** win. Mixing versions in one consumer reason, one audit claim, or one fairness statement is a conflict.

## Conflicts

Stop and escalate when:

- a vendor document is older than the approved current version;
- two vendor versions are both marked current;
- vendor wording disagrees with 12 CFR 1002.9’s specific-principal-reason rule (generic, score-only, or internal-policy reasons);
- this synthetic packet is used after a real vendor packet has been accepted;
- a vendor claims production fairness or legal compliance this packet cannot support.

Do not resolve a conflict by paraphrasing, nearest-match mapping, or keeping the stale file “for convenience.”
