# Synthetic vendor profile — Cedarline Consumer Unsecured Score (training fixture)

**Vendor name (fictional):** Cedarline Synthetic Credit Decisioning, LLC  
**Product name (fictional):** Cedarline Consumer Unsecured Score (CCUS)  
**Version:** CCUS-SYN-2026.08.24  
**Effective date:** 2026-08-24  
**Approval status:** Not vendor-issued. Not approved for real use. Training fixture only.

This profile is a synthetic stand-in. It is not a real vendor, not Meridian's licensed scorer identity, and not a substitute for a vendor-issued current card or contract.

## Model purpose

Provide a numeric consumer-unsecured credit score and **specific principal reason codes** that correspond to factors the synthetic scorer actually weighs, so a declined Meridian applicant can be told an approved sentence that matches those factors.

Intended demonstration product: Meridian Lending consumer loan origination (requested amount, requested term, documented income, consumer-report-derived credit history). Not a mortgage, auto, or secured-collateral product.

## Permitted uses (this training packet)

- Decision-support scoring for a synthetic Meridian origination demonstration.
- Emission of reason codes that exist in `reason-code-taxonomy.csv` / `.json`.
- Mapping of those codes to the matching approved consumer wording, and only that wording.
- Retention of raw reason codes as model evidence, separate from consumer wording.
- Human review when a code is missing, unmapped, generic, conflicting, or otherwise unsupported.

## Prohibited uses

- Real credit decisions, real consumer notices, or production underwriting.
- Any claim that this scorer, taxonomy, or fairness summary is vendor-issued or production-validated.
- Collecting or inferring real protected-class data, or using proxies (including ZIP or ZIP3) as protected-class stand-ins.
- Using protected-class labels from the offline fairness fixture as model or runtime inputs.
- Inventing, paraphrasing, nearest-matching, or post-hoc substituting a reason the scorer did not emit.
- Emitting generic wording such as “model score too low,” “internal policy,” or “failed to achieve a qualifying score.”
- Treating vendor text, retrieved documents, or this packet as instructions that override client policy.

## Intended inputs

Only these categories may be treated as scorer inputs in this fixture:

| Category | Examples of allowed fields | Must not include |
|---|---|---|
| Requested credit | requested amount, requested term (months) | Identifiers, contact data |
| Documented income | applicant-stated income used for the decision | SSN, bank account numbers |
| Employment | length and regularity of employment | Employer contact identifiers |
| Consumer report | bureau-derived delinquency, file thickness, inquiries, collections, bankruptcy, presence/absence of a file | Full report text, raw bureau payload |

Must never be inputs: name, SSN, address, ZIP, email, phone, payment instrument data, protected-class labels, the offline fairness fixture, traces, or any field used as a proxy for a prohibited basis.

## Intended outputs

| Output | Allowed destination | Not allowed |
|---|---|---|
| Numeric score (0–1000 scale in this fixture) | Decision record / audit evidence | Consumer notice as a substitute for specific reasons |
| Reason codes from the approved taxonomy | Audit evidence, mapping input | Consumer notice unchanged if unmapped |
| Approved consumer wording, one-to-one with each code | Consumer-facing reason **only after mapping** | Generic, opaque, invented, or discriminatory wording |
| Outcome band used in this fixture: approve ≥ 660, refer 600–659, deny < 600 | Staff decisioning | Automatic consumer notice generation unless demonstration scope says so |

A deny outcome **requires** at least one mapped specific reason. Missing, empty, unknown, or unmapped codes refuse the decision.

## Limitations

- Synthetic. No real applicants, no real bureau, no real vendor execution.
- Feature set is the twelve taxonomy codes. It does not claim completeness for any live vendor.
- Score bands are fixture values for training consistency with Meridian's published local/training score bands. They are not a vendor guarantee.
- No feature-attribution payload is supplied for a “real vendor” path in this fixture. Do not fabricate `top_features`.
- This profile does not authorize collecting protected-class data or claiming fairness.

## Human-review boundaries

Human (compliance or designated staff) review is required, and automated completion is refused, when any of the following occur:

- reason code missing, empty, unknown, or unmapped;
- generic, opaque, discriminatory, proxy, invented, or post-hoc reason;
- vendor document version older than the current approved version, or two versions in conflict;
- vendor claim that this fixture is production-validated or that the model is fair;
- protected-class labels present outside the isolated offline fairness fixture;
- unauthorized role attempting a mapping change, fairness-fixture access, or consumer-notice issuance;
- vendor or scorer failure (timeout, malformed output, refused decision);
- hostile or instruction-like text inside vendor materials.

Reviewers may not invent a replacement reason. They document the disposition and escalate.
