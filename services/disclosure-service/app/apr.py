"""APR + finance-charge calculation (Reg Z / TILA disclosure box).

Two defects were found here by giving the tests an independent oracle instead
of a reference that re-implements this file's own formula (Week 1-4 client
review: "your one test vector's expected value mirrors the implementation, so
it cannot catch a wrong implementation"). Both are fixed below; both had been
shipping on real disclosures.

**1. `compute_apr` was not the actuarial method, despite saying it was.**
It computed a simple add-on ratio:

    apr = (finance_charge / amount_financed) / (term_months / 12) * 100

That prices the finance charge as if the whole amount financed stayed
outstanding for the entire term. It does not -- the balance amortizes, so the
borrower has the use of less money as time passes and the true rate is
materially higher. Reg Z Appendix J requires the *actuarial* rate: the rate at
which the present value of the payment stream equals the amount financed. For
this repo's own test vector (18,000 at 7.99% over 48 months) the ratio gave
**5.196%** against an actuarial **9.584%** -- a 4.39 percentage-point
understatement, 35x the 0.125pp tolerance, in the direction that flatters the
loan. The earlier fee-constant fix (0.025 -> 0.030, worth 0.155pp) was a real
correction sitting on top of a wrong formula.

**2. `finance_charge` excluded the prepaid finance charge.**
It returned `payments - principal`, i.e. interest only. Under Reg Z the
origination fee is a prepaid finance charge: it belongs *in* the finance charge
and comes *out of* the amount financed. Excluding it broke the identity every
TILA box must satisfy --

    amount financed + finance charge == total of payments

-- by exactly the fee. `test_apr.py` now asserts that identity directly, which
is the cheapest possible check that the box is internally coherent.

Decimal throughout; float only at the API/display boundary.
"""
from decimal import Decimal

from .fees import ORIGINATION_FEE_PCT

# Bisection bracket for the monthly actuarial rate. 100%/month is far above
# anything this system can originate and keeps the root bracketed even for a
# pathological fee/term combination.
_RATE_LO = Decimal("0")
_RATE_HI = Decimal("1")
# 200 halvings put the monthly rate far below the precision a 3-decimal annual
# figure can express, so the disclosed result is exact as rounded.
_BISECTION_STEPS = 200


def _to_decimal(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def monthly_payment_decimal(principal, annual_rate_pct, term_months: int) -> Decimal:
    """Same calculation as monthly_payment(), without the float cast at the end --
    for callers (schedule.py) that need to keep accumulating in Decimal across
    many rows instead of round-tripping through float once per call."""
    p = _to_decimal(principal)
    r = _to_decimal(annual_rate_pct) / 100 / 12
    if r == 0:
        return p / term_months
    factor = (1 + r) ** term_months
    return p * r * factor / (factor - 1)


def monthly_payment(principal, annual_rate_pct, term_months: int) -> float:
    return float(monthly_payment_decimal(principal, annual_rate_pct, term_months))


def prepaid_finance_charge_decimal(principal) -> Decimal:
    """The origination fee. A prepaid finance charge under Reg Z: part of the
    finance charge, and withheld from what the borrower actually receives."""
    return _to_decimal(principal) * ORIGINATION_FEE_PCT


def amount_financed_decimal(principal) -> Decimal:
    """What the borrower actually gets: principal less prepaid finance charges."""
    return _to_decimal(principal) - prepaid_finance_charge_decimal(principal)


def finance_charge_decimal(principal, annual_rate_pct, term_months: int) -> Decimal:
    """Total cost of credit: interest over the term PLUS prepaid finance charges.

    Written as `total_of_payments - amount_financed` so the TILA box cannot
    fail to foot, rather than as `interest + fee` computed separately.
    """
    pmt = monthly_payment_decimal(principal, annual_rate_pct, term_months)
    return pmt * term_months - amount_financed_decimal(principal)


def finance_charge(principal, annual_rate_pct, term_months: int) -> float:
    return float(finance_charge_decimal(principal, annual_rate_pct, term_months))


def _present_value(payment: Decimal, monthly_rate: Decimal, term_months: int) -> Decimal:
    """Present value of a level monthly annuity at `monthly_rate`."""
    if monthly_rate == 0:
        return payment * term_months
    return payment * (1 - (1 + monthly_rate) ** -term_months) / monthly_rate


def actuarial_monthly_rate(amount_financed: Decimal, payment: Decimal, term_months: int) -> Decimal:
    """The monthly rate i where PV(payment stream at i) == amount financed.

    Reg Z Appendix J's actuarial method. Bisection rather than Newton-Raphson:
    PV is strictly decreasing in i across the bracket, so bisection cannot
    diverge and needs no derivative, and it is deterministic -- identical
    inputs give bit-identical output every run, which matters for a number that
    goes on a disclosure.
    """
    if term_months <= 0 or payment <= 0 or amount_financed <= 0:
        return Decimal(0)
    # No finance charge at all (no interest, no fee) -> the rate is zero.
    if payment * term_months <= amount_financed:
        return Decimal(0)

    lo, hi = _RATE_LO, _RATE_HI
    for _ in range(_BISECTION_STEPS):
        mid = (lo + hi) / 2
        if _present_value(payment, mid, term_months) > amount_financed:
            lo = mid          # rate too low: PV still exceeds the advance
        else:
            hi = mid
    return (lo + hi) / 2


def compute_apr(principal, annual_rate_pct, term_months: int) -> float:
    """The disclosed APR, actuarial method, rounded to 3 decimals.

    Property worth knowing when reading a disclosure produced here: with no
    prepaid finance charge the APR equals the note rate exactly, because the
    payment stream is then priced at precisely that rate. Everything above the
    note rate is the fee amortized across the term. `test_apr.py` asserts that
    identity across several rates and terms -- it holds for mathematical
    reasons independent of anything in this file, so it is a real check rather
    than a mirror of the implementation.
    """
    af = amount_financed_decimal(principal)
    pmt = monthly_payment_decimal(principal, annual_rate_pct, term_months)
    monthly = actuarial_monthly_rate(af, pmt, term_months)
    return float(round(monthly * 12 * 100, 3))


def note_rate_from_payment(principal, payment, term_months: int) -> float:
    """Recover the note rate from a stored payment, principal and term.

    The offers row keeps the APR but not the note rate, and the two are not
    interchangeable once a prepaid fee exists -- the APR is solved against the
    amount financed, the payment schedule runs on the full principal. The read
    path used to redisplay the amortization schedule at the APR, which produced
    a schedule whose payments did not match the disclosed monthly payment. That
    was wrong before this module's APR fix too (it just missed in the other
    direction); correcting the APR made it visible.

    Same solver as the APR, run against the full principal instead of the
    amount financed, which by definition gives the rate the payments were
    actually calculated at.
    """
    monthly = actuarial_monthly_rate(_to_decimal(principal), _to_decimal(payment), term_months)
    return float(monthly * 12 * 100)
