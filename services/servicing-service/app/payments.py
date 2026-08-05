"""Payment handling (formerly the vendor's prototype 'pay.py').

There is NO idempotency key — a retried POST inserts a second payments row and
applies the amount twice (double-charge). (D2, #4, #7 -- unrelated to the PCI
fix below, left as-is; same scope boundary payment-service's own idempotency
fix drew for its equivalent debt.)

ADR 0008 (Week 5 tokenization): this used to receive and store the FULL PAN
and CVV on the payments row, and log the full charge request (PAN, CVV, SSN)
at INFO with zero redaction (D5) -- this duplicate, legacy endpoint just
hadn't been ported to the same fix payment-service's own /payments already
got. Card capture tokenizes at the processor -- this never receives a raw
PAN/CVV/SSN at all anymore, only an opaque processor_token (accepted but not
persisted -- there's nothing here that needs it after the fact) plus
last4/brand for display.
"""
from .logging_config import get_logger
from . import db, balance

log = get_logger("payment")   # writes to logs/payment-service.log


def charge(loan_id: int, processor_token: str, last4: str, brand: str, amount: float,
           name: str = None, method: str = "card") -> dict:
    # No raw PAN/CVV/SSN ever reaches this point (ADR 0008) -- nothing left
    # here that needs redacting before logging.
    log.info(
        "POST /payments charge loan_id=%s amount=%s method=%s -> ok",
        loan_id, amount, method,
    )
    # No idempotency check. No unique charge reference. Every POST inserts a row.
    db.query(
        "INSERT INTO payments (loan_id, last4, brand, amount, method) "
        "VALUES (%s, %s, %s, %s, %s)",
        (loan_id, last4, brand, float(amount), method),
    )
    new_balance = balance.apply_payment(loan_id, amount)
    return {"loan_id": loan_id, "amount": amount, "balance": new_balance}
