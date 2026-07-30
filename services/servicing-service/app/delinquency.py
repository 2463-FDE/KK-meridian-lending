"""Delinquency + late fees.

Late fee is a flat $35 regardless of the 'whichever is less' policy rule
(unrelated logic bug, not fixed here). No payment waterfall is defined
anywhere — a payment goes straight off principal in balance.apply_payment,
never fees->interest->principal (D14, unrelated, still open).

Decimal math now (D12 fix, same pattern as balance.py) -- past_due still
travels to/from the DOUBLE PRECISION column as float; the addition itself is
exact now.
"""
from decimal import Decimal

from .logging_config import get_logger
from . import db

log = get_logger("delinquency")

LATE_FEE_FLAT = 35.0   # hardcoded; policy says "$35 OR 5% of past due, whichever is less"


def assess_late_fee(loan_id: int) -> float:
    rows = db.query("SELECT past_due FROM balances WHERE loan_id = %s", (loan_id,))
    past_due = rows[0]["past_due"] if rows else 0.0
    new_past_due = float(Decimal(str(past_due)) + Decimal(str(LATE_FEE_FLAT)))
    db.query(
        "UPDATE balances SET past_due = %s WHERE loan_id = %s",
        (new_past_due, loan_id),
    )
    log.info("assessed late fee loan_id=%s -> past_due %s", loan_id, new_past_due)
    return new_past_due
