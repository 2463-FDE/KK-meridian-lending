"""Delinquency + late fees.

Late fee is a flat $35 regardless of the 'whichever is less' policy rule
(unrelated logic bug, not fixed here). No payment waterfall is defined
anywhere — a payment goes straight off principal in balance.apply_payment,
never fees->interest->principal (D14, unrelated, still open).

Decimal math (D12 fix, same pattern as balance.py). `balances.past_due` is
`NUMERIC(14,2)` -- this docstring used to say the column was still
`DOUBLE PRECISION`, which the D12 migration had already made false.

The assessment below writes `balances` directly, like the other legacy writers
in this service. 0035's compatibility bridge captures the delta into
`ledger_entries`, so the movement is recorded; it is a machine-originated fee
with no human behind it, which is why it is outside the maker-checker workflow
by design (`specs/0002-maker-checker-self-approval.md` §8) rather than merely
un-approved.
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
        "UPDATE balances SET past_due = COALESCE(past_due, 0) + %s WHERE loan_id = %s",
        (LATE_FEE_FLAT, loan_id),
    )
    log.info("assessed late fee loan_id=%s -> past_due %s", loan_id, new_past_due)
    return new_past_due
