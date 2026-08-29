# Meridian Lending — Fee Schedule (internal)

*Last reviewed: 2024-11. Owner: Lending Ops.*

> ⚠️ These published values are the source of truth. Note that the code hardcodes its
> own copies of several of these in `apr.py`, `fees.py`, and `offer.py`, and they have
> drifted from this schedule.

| Fee | Amount |
|-----|--------|
| Origination fee | 3.0% of principal |
| Late payment fee | **Decided 2026-08-29.** At most **one fee per missed scheduled installment**, after the grace period, never reassessed against the same installment: the lesser of **$35.00** and **5% of that installment's unpaid scheduled principal + interest**. Previous late fees and all other fees are excluded from the base. *The code does not yet implement this — see "Current implementation differs" below.* |
| Returned payment (NSF) | $25 |
| Payoff statement | $0 |

### Current implementation differs — late payment fee

**The row above is the policy. This section is what the code does today, and the
two are not the same.** Recorded here rather than left for a reader to discover,
because this file is the source of truth *and* it is served to policy chat: an
answer quoting the old rule as current policy would be wrong in front of a
client.

`servicing-service/app/delinquency.py` computes `min($35, 5% of
balances.past_due)`. `past_due` is one projected total mixing principal,
interest and every fee already assessed — so the base is wider than the decided
rule allows, and a posted fee raises it, which is the compounding the client
asked about at the 2026-08-19 demo. There is also no per-installment cap of any
kind: nothing records which installment a fee belongs to, so nothing can refuse
a second one.

Measured against the decided policy, that can charge **more** than is due, in
those two independent ways. It is bounded and legible rather than silent — $35
per assessment at most, every fee on the immutable ledger, reversible by waiver
— and the assessment route has no scheduler behind it, so a repeat requires a
person to ask for it.

**Why it is not simply corrected.** The decided rule needs installment-level
facts this system does not persist: nothing records which installment a payment
satisfied, or which installment a fee belongs to, so "unpaid scheduled principal
and interest for that installment" is not derivable. `docs/DEBT.md` D23 states
the exact missing primitive, the smallest data-model addition that would close
it, and why no backfill of existing loans could be truthful.

**It will not be approximated from the past-due total.** A number that resembles
the decided rule without being it is worse than one that is legibly the older
published rule, because only the second is obviously not the new policy.

Worked examples of the decided rule, supplied with the decision: unpaid
scheduled P&I of $200 → $10, $500 → $25, $700 → $35 (cap), $1000 → $35 (cap).

## APR / finance charge

- APR is the annualized cost of credit including the finance charge per Reg Z.
- Disclosed APR and finance charge are subject to **Reg Z tolerances**; a disclosed APR
  that differs from the actual by more than the regulatory tolerance is a violation.
- The payment waterfall on a received payment is: **fees → accrued interest → principal.**

## Interest

- Interest accrues on the outstanding principal at the loan's note rate.
- Standard personal-installment note rates: 7.99% – 24.99% APR by risk band.
