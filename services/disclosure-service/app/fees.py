"""Fee constants.

Single source of truth for the origination fee — apr.py and offer.py both
import ORIGINATION_FEE_PCT from here instead of redeclaring their own copy.
That copy-paste was exactly how apr.py's copy drifted to 0.025 against the
published 3.0% (D6). Published source: policies/fee_schedule.md.
"""
from decimal import Decimal

ORIGINATION_FEE_PCT = Decimal("0.030")   # policy: 3.0%
LATE_FEE_FLAT = Decimal("35.0")
NSF_FEE = Decimal("25.0")


def origination_fee(amount) -> float:
    p = amount if isinstance(amount, Decimal) else Decimal(str(amount))
    return float(p * ORIGINATION_FEE_PCT)


def late_fee(past_due) -> float:
    # "flat $35 OR 5% of past due, whichever is less" -- but this returns the
    # flat fee only. Separate, pre-existing logic bug, not part of the
    # Decimal/fee-drift fix here.
    return float(LATE_FEE_FLAT)
