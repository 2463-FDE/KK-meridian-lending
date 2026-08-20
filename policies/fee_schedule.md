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

> **Open question, recorded rather than answered (`docs/DEBT.md` D23).** A posted
> late fee raises the past-due amount, so a second assessment prices five per
> cent off a base that already contains the first fee — repeated assessment
> compounds. This page does not say whether that is intended, and the code does
> not guess: it implements the comparison above exactly as published. Lending
> Operations owns the answer. Until there is one the behaviour stays as it is,
> recorded in D23 rather than changed.

## APR / finance charge

- APR is the annualized cost of credit including the finance charge per Reg Z.
- Disclosed APR and finance charge are subject to **Reg Z tolerances**; a disclosed APR
  that differs from the actual by more than the regulatory tolerance is a violation.
- The payment waterfall on a received payment is: **fees → accrued interest → principal.**

## Interest

- Interest accrues on the outstanding principal at the loan's note rate.
- Standard personal-installment note rates: 7.99% – 24.99% APR by risk band.
