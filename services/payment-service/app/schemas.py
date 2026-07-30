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
    # Review fix: required so a retry/double-click can be recognized as the
    # SAME request instead of charging twice -- see payments.py::charge() and
    # db/migrations/0009's partial unique index. Caller-generated (e.g. a
    # UUID minted once per submit attempt, reused on retry).
    idempotency_key: str = Field(min_length=1, max_length=255)


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
