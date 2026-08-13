# Meridian Lending — Underwriting Guidelines (internal)

*Last reviewed: 2024-11. Owner: Lending Ops.*

## Eligibility

- Minimum age: 18.
- US residency / valid SSN or ITIN required.
- Loan amount: $1,000 – $50,000 (Personal Installment).
- Term: 12 / 24 / 36 / 48 / 60 months.

## Credit decisioning

1. Pull credit (Experian) and obtain a score.
2. Run the risk model to produce a model score (0–850 scale) and a decision band.
3. Apply policy cutoffs:
   - **Approve:** model score ≥ 660.
   - **Refer (manual review):** model score 600–659.
   - **Deny:** model score < 600.
4. Counteroffer is permitted (lower amount / shorter term) when score is in the refer band.

> **This section previously published DTI and fraud-flag cutoffs that the system
> has never applied** — "DTI ≤ 43%", "DTI 43–50%", "DTI > 50%, or fraud flag".
> They were removed rather than implemented, and `adr/0007` records that decision
> and why. The short version: the system does not collect a debt figure, so every
> DTI it could compute would be income-only and therefore not a DTI at all, and a
> fabricated ratio inside a denial reason is worse than a policy that overpromises.
>
> **This is a documentation correction, not a change of lending policy.** No
> applicant is decided differently because of it. If Lending Ops wants a real DTI
> cutoff, it starts with collecting monthly debt obligations — see the *Not
> currently applied* section below.

`db/tests/test_policy_matches_implemented_cutoffs.py` holds this list to
`decision-service/app/decision.py`. A cutoff published here that the code does not
apply fails that test.

## Adverse action (Reg B)

When an application is denied or counter-offered and not accepted, an adverse-action
notice must be sent stating the **specific principal reason(s)**. Timing:
- 30 days for a completed application.
- 30 days for an incomplete application or existing account.
- 90 days after a counteroffer that is not accepted.

> Operational note: the tool currently records the *outcome* of a decision but the
> reasons are produced ad hoc at letter-generation time. (Flagged for review.)

## Debt-to-income (DTI) — defined, not currently applied

DTI = total monthly debt obligations ÷ gross monthly income. Include the new loan's
estimated monthly payment.

**No decision in this system uses it.** The definition is kept because it is the
one Lending Ops intends and a future implementation should use it — not because
anything computes it today.

What implementing it would require, so the gap is a scope rather than a mystery:

1. **Collect monthly debt obligations.** Intake captures gross income and nothing
   about existing debt. Without that figure there is no numerator, and the
   alternative — inferring it from the bureau pull's tradelines — is a different
   and larger piece of work with its own accuracy questions.
2. **Decide the boundary semantics.** "DTI ≤ 43%" and "DTI 43–50%" overlap at
   exactly 43%. A published cutoff that contradicts itself on the boundary is the
   kind of ambiguity that produces two applicants with identical files and
   different answers.
3. **Give it an adverse-action reason.** Reg B requires the specific principal
   reason for a denial. A DTI denial that reports a generic reason is a compliance
   defect, not a partial implementation.

## Fraud flags — not currently applied

The deny band previously listed "or fraud flag". There is no fraud-flag input, no
provider integration and no field on the application, so nothing could raise one.
Removed for the same reason as the DTI cutoffs, and it needs the same treatment: a
source for the signal before a rule that acts on it.

## Records retention

Applications and adverse-action records are retained per Reg B (~25 months). Financial
records are retained per SOX. Do not delete these even on customer request without
Compliance sign-off.
