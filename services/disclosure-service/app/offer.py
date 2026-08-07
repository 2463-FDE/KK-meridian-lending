"""Offer assembly.

ORIGINATION_FEE_PCT used to be a third hardcoded copy here (0.03), independent
of fees.py (0.030) and apr.py's since-fixed 0.025 -- same idea, three numbers.
Now imported from fees.py, the single source of truth.
"""
from decimal import Decimal

from . import apr, schedule
from .fees import ORIGINATION_FEE_PCT

__all__ = ["ORIGINATION_FEE_PCT", "build_offer"]


def build_offer(principal: float, annual_rate_pct: float, term_months: int) -> dict:
    """The five canonical TILA amounts for one loan.

    Every figure comes from apr.py's Decimal functions rather than being
    recomputed here. The amount financed in particular used to be derived
    locally (`p - p * ORIGINATION_FEE_PCT`) while the finance charge came from
    apr.py -- two places deciding what the fee is, which is the same
    duplicate-constant shape that caused the original fee drift. One source now.
    """
    # Derived from the CONTRACTUAL cash flows -- the payment schedule the
    # borrower actually receives, whose final payment differs because it absorbs
    # the cent residue. The previous version multiplied the unrounded payment by
    # the term, so on a 15,000/48mo loan the disclosed total exceeded the
    # schedule's own sum by 0.16 and the finance charge inherited that error.
    af = apr.amount_financed_decimal(principal)
    rows = schedule.amortization(principal, annual_rate_pct, term_months)
    payments = [Decimal(str(r["payment"])) for r in rows]
    total = sum(payments)
    return {
        "apr": apr.apr_from_cash_flows(af, payments),
        "finance_charge": round(float(total - af), 2),
        "monthly_payment": float(payments[0]),
        # The final payment differs; a disclosure that shows only one figure
        # cannot explain the schedule it is attached to.
        "final_payment": float(payments[-1]),
        "regular_payment_count": max(term_months - 1, 0),
        "amount_financed": round(float(af), 2),
        "total_of_payments": round(float(total), 2),
    }
