"""Pydantic models for the Payment Service API."""
from typing import Optional

from pydantic import BaseModel, Field

# Review fix: amount was an unconstrained float -- a negative value credited
# the borrower's balance instead of charging them (servicing computes
# new_balance = current - amount), and zero/NaN/Infinity all passed through
# uncaught. gt=0 alone already rejects NaN (`float('nan') > 0` is False in
# Python) and negative/zero; le=_MAX_AMOUNT also catches Infinity (`inf <= x`
# is False) and caps a single charge at a sane ceiling.
_MAX_AMOUNT = 1_000_000.00


class PaymentIn(BaseModel):
    loan_id: int
    pan: Optional[str] = None
    cvv: Optional[str] = None
    amount: float = Field(gt=0, le=_MAX_AMOUNT)
    ssn: Optional[str] = None
    name: Optional[str] = None
    method: str = "card"
    # Review fix: caller-supplied, optional so existing callers aren't broken.
    # When present, a retry with the same key is a safe no-op (see
    # payments.charge) instead of a second charge -- backed by the partial
    # unique index in db/migrations/0007_payments_idempotency_key.sql.
    idempotency_key: Optional[str] = Field(default=None, max_length=200)


class PaymentOut(BaseModel):
    payment_id: Optional[int] = None
    loan_id: int
    status: str
    applied_amount: float


class PaymentItem(BaseModel):
    id: int
    amount: float
    method: Optional[str] = None
    masked_pan: Optional[str] = None
    created_at: Optional[str] = None
