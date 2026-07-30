"""Balance + payment application.

Arithmetic now runs in Decimal internally (D12 fix, same pattern already
applied to disclosure-service's apr.py) -- values still travel to/from the
DOUBLE PRECISION columns as float; only the computation itself is exact now,
so repeated payments/adjustments no longer accumulate float drift. The DB
column type itself is unchanged here -- a real schema migration
(DOUBLE PRECISION -> NUMERIC) is a separate, bigger step, not done in this pass.

The read-modify-write here still has no lock (D3, unrelated to this fix, still
open) and there is still no payment waterfall -- fees/interest/principal
(D14, unrelated, still open).
"""
from decimal import Decimal

from .logging_config import get_logger
from . import db

log = get_logger("balance")


def _to_decimal(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def get_balance(loan_id: int) -> float:
    rows = db.query("SELECT balance FROM balances WHERE loan_id = %s", (loan_id,))
    return rows[0]["balance"] if rows else 0.0


def get_past_due(loan_id: int) -> float:
    rows = db.query("SELECT past_due FROM balances WHERE loan_id = %s", (loan_id,))
    return rows[0]["past_due"] if rows else 0.0


def apply_payment(loan_id: int, amount: float) -> float:
    """Read-modify-write with no lock (D3). Decimal math now (D12 fix). No
    waterfall -- straight off principal (D14)."""
    current = get_balance(loan_id)                                       # READ
    new_balance = float(_to_decimal(current) - _to_decimal(amount))      # MODIFY, exact
    db.query(                                                            # WRITE (overwrite in place)
        "UPDATE balances SET balance = %s, updated_at = now() WHERE loan_id = %s",
        (new_balance, loan_id),
    )
    log.info("applied payment loan_id=%s balance %s -> %s", loan_id, current, new_balance)
    return new_balance


def adjust_balance(loan_id: int, new_value: float) -> float:
    """Set the balance directly. No ledger entry; the prior value is gone forever."""
    current = get_balance(loan_id)
    new_balance = float(_to_decimal(new_value))
    db.query(
        "UPDATE balances SET balance = %s, updated_at = now() WHERE loan_id = %s",
        (new_balance, loan_id),
    )
    log.info("adjusted balance loan_id=%s %s -> %s", loan_id, current, new_value)
    return new_balance


def waive_fee(loan_id: int, amount: float) -> float:
    """Reduce past_due. Read-modify-write, no lock -- races with apply_payment (D3)."""
    rows = db.query("SELECT past_due FROM balances WHERE loan_id = %s", (loan_id,))
    past_due = rows[0]["past_due"] if rows else 0.0
    new_past_due = float(_to_decimal(past_due) - _to_decimal(amount))
    db.query(
        "UPDATE balances SET past_due = %s, updated_at = now() WHERE loan_id = %s",
        (new_past_due, loan_id),
    )
    log.info("waived fee loan_id=%s past_due %s -> %s", loan_id, past_due, new_past_due)
    return new_past_due
