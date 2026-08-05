"""Offer assembly.

ORIGINATION_FEE_PCT used to be a third hardcoded copy here (0.03), independent
of fees.py (0.030) and apr.py's since-fixed 0.025 -- same idea, three numbers.
Now imported from fees.py, the single source of truth.
"""
from decimal import Decimal

from . import apr
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
    a = apr.compute_apr(principal, annual_rate_pct, term_months)
    fc = apr.finance_charge_decimal(principal, annual_rate_pct, term_months)
    pmt_d = apr.monthly_payment_decimal(principal, annual_rate_pct, term_months)
    af = apr.amount_financed_decimal(principal)
    total = pmt_d * term_months
    return {
        "apr": a,
        "finance_charge": round(float(fc), 2),
        "monthly_payment": round(float(pmt_d), 2),
        "amount_financed": round(float(af), 2),
        "total_of_payments": round(float(total), 2),
    }
