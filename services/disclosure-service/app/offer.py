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
    a = apr.compute_apr(principal, annual_rate_pct, term_months)
    fc = apr.finance_charge(principal, annual_rate_pct, term_months)
    pmt = apr.monthly_payment(principal, annual_rate_pct, term_months)
    p = Decimal(str(principal))
    fee = p * ORIGINATION_FEE_PCT
    return {
        "apr": a,
        "finance_charge": round(fc, 2),
        "monthly_payment": round(pmt, 2),
        "amount_financed": round(float(p - fee), 2),
        "total_of_payments": round(pmt * term_months, 2),
    }
