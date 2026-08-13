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
    db.query(
        "UPDATE balances SET balance = balance - %s, updated_at = now() WHERE loan_id = %s",
        (amount, loan_id),
    )
    log.info("applied payment loan_id=%s balance %s -> %s", loan_id, current, new_balance)
    return new_balance


def apply_payment_once(payment_id: int, loan_id: int, amount: float) -> tuple[float, bool]:
    """Review fix: apply_payment() above has no idempotency of its own -- it
    trusted payment-service to never call apply-payment twice for the same
    payment. payment-service now retries a pending apply on a same-key retry
    (db/migrations/0012), so that trust has to be a real guarantee instead:
    calling this twice for the same payment_id must move the balance once.

    payment_applications' PK on payment_id is the atomic guard -- the INSERT
    only lands a row for whichever call gets there first; only that call goes
    on to actually move the balance. Returns (balance, applied) so the caller
    can tell a genuine apply from a no-op replay.

    Review fix: the marker INSERT and the balance UPDATE must commit or roll
    back together. Each used to be its own auto-committed statement, so if
    apply_payment()'s UPDATE errored or timed out AFTER the marker had already
    landed, the marker was permanent but the balance never moved -- every
    retry for this payment_id then hit the ON CONFLICT path and silently
    skipped the apply forever (money captured, loan never credited). Both
    statements now run inside one transaction (db.transaction()), through the
    cursor it yields -- not apply_payment()/db.query(), which run on a
    separate, shared autocommit connection and so would run outside this
    transaction entirely: if the UPDATE raises, the marker rolls back with
    it, so a retry sees no marker and genuinely retries the apply instead of
    skipping it.
    """
    with db.transaction() as cur:
        cur.execute(
            "INSERT INTO payment_applications (payment_id, loan_id, amount) "
            "VALUES (%s, %s, %s) ON CONFLICT (payment_id) DO NOTHING RETURNING payment_id",
            (payment_id, loan_id, amount),
        )
        if not cur.fetchall():
            log.info(
                "apply-payment payment_id=%s already applied -- skipping duplicate apply",
                payment_id,
            )
            return get_balance(loan_id), False

        cur.execute(
            "INSERT INTO ledger_entries "
            "(loan_id, component, amount, entry_type, payment_id) "
            "VALUES (%s, 'principal', -%s, 'payment', %s)",
            (loan_id, amount, payment_id),
        )
        cur.execute("SELECT balance FROM balances WHERE loan_id = %s", (loan_id,))
        rows = cur.fetchall()
        if not rows:
            raise LookupError(f"no balances row for loan_id={loan_id}")
        new_balance = rows[0]["balance"]
        log.info("applied payment loan_id=%s new_balance=%s", loan_id, new_balance)
    return new_balance, True


def adjust_balance(loan_id: int, new_value: float) -> float:
    """Set the balance directly. No ledger entry; the prior value is gone forever."""
    current = get_balance(loan_id)
    new_balance = float(_to_decimal(new_value))
    with db.transaction() as cur:
        cur.execute("SELECT balance FROM balances WHERE loan_id = %s FOR UPDATE", (loan_id,))
        rows = cur.fetchall()
        if not rows:
            raise LookupError(f"no balances row for loan_id={loan_id}")
        delta = _to_decimal(new_balance) - _to_decimal(rows[0]["balance"])
        if delta:
            cur.execute(
                "UPDATE balances SET balance = balance + %s, updated_at = now() WHERE loan_id = %s",
                (delta, loan_id),
            )
    log.info("adjusted balance loan_id=%s %s -> %s", loan_id, current, new_value)
    return new_balance


def waive_fee(loan_id: int, amount: float) -> float:
    """Reduce past_due. Read-modify-write, no lock -- races with apply_payment (D3)."""
    rows = db.query("SELECT past_due FROM balances WHERE loan_id = %s", (loan_id,))
    past_due = rows[0]["past_due"] if rows else 0.0
    new_past_due = float(_to_decimal(past_due) - _to_decimal(amount))
    db.query(
        "UPDATE balances SET past_due = COALESCE(past_due, 0) - %s, updated_at = now() WHERE loan_id = %s",
        (amount, loan_id),
    )
    log.info("waived fee loan_id=%s past_due %s -> %s", loan_id, past_due, new_past_due)
    return new_past_due
