# Meridian Lending — Fee Schedule (internal)

*Last reviewed: 2024-11. Owner: Lending Ops.*

> ⚠️ These published values are the source of truth. Note that the code hardcodes its
> own copies of several of these in `apr.py`, `fees.py`, and `offer.py`, and they have
> drifted from this schedule.

| Fee | Amount |
|-----|--------|
| Origination fee | 3.0% of principal |
| Late payment fee | $35 flat, or 5% of the past-due amount, whichever is **less** |
| Returned payment (NSF) | $25 |
| Payoff statement | $0 |

> **Answered 2026-08-29, and NOT yet implemented — read both halves
> (`docs/DEBT.md` D23).** The open question this block used to carry was whether
> repeated assessment compounding was intended. It has been decided, and the
> answer replaces the rule rather than adjusting it:
>
> - at most **one** late fee per missed scheduled installment, after the existing
>   grace period, never reassessed against the same installment; a later
>   installment that separately becomes overdue may take one of its own;
> - the amount is `min($35.00, 5% × unpaid scheduled PRINCIPAL + INTEREST for
>   that installment)`;
> - **previous late fees and every other fee are excluded from the percentage
>   base.** The base is one installment's scheduled principal and interest — not
>   the past-due total, which mixes principal, interest and fees.
>
> Worked examples given with the decision: unpaid scheduled P&I of $200 → $10,
> $500 → $25, $700 → $35 (cap), $1000 → $35 (cap).
>
> **The Late payment fee row above still describes what the code does**, which is
> the older published comparison priced off the past-due total. That is deliberate
> and is why this block says "not yet implemented": the decided rule needs
> installment-level facts this system does not persist — nothing records which
> installment a payment satisfied, or which installment a fee belongs to — so
> implementing it truthfully requires a data-model change rather than an edit to
> the fee calculation. D23 states the exact missing primitive, the smallest
> addition that would close it, and why a backfill of existing loans could not be
> truthful. **The code will not be changed to approximate the decided rule from
> the past-due total**, because a number that looks like the new rule and is not
> it is worse than one that is legibly the old one.

## APR / finance charge

- APR is the annualized cost of credit including the finance charge per Reg Z.
- Disclosed APR and finance charge are subject to **Reg Z tolerances**; a disclosed APR
  that differs from the actual by more than the regulatory tolerance is a violation.
- The payment waterfall on a received payment is: **fees → accrued interest → principal.**

## Interest

- Interest accrues on the outstanding principal at the loan's note rate.
- Standard personal-installment note rates: 7.99% – 24.99% APR by risk band.
