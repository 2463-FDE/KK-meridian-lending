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
    # ADR 0008 (Week 5 tokenization fix): this endpoint used to accept raw
    # pan/cvv/ssn directly -- pan/cvv is an unconditional PCI-DSS violation to
    # store (CVV especially, no "encrypted at rest" exception exists), and ssn
    # had no functional role in a card/ACH charge at all. Card capture now
    # tokenizes at the processor (see frontend/lib/tokenize.ts and
    # specs/0001-online-payments-idempotency-tokenization.md Part 2) --
    # payment-service never receives a raw PAN/CVV/SSN, only the processor's
    # own opaque token plus non-sensitive display fields. `extra="forbid"`
    # makes that a real rejection, not just "the field is unused" -- a client
    # still sending pan/cvv/ssn gets a 422, not a silent drop, so the wire
    # contract is provably enforced, not just conventionally followed.
    model_config = {"extra": "forbid"}

    loan_id: int
    processor_token: str = Field(min_length=1, max_length=255)
    last4: str = Field(min_length=4, max_length=4, pattern=r"^\d{4}$")
    brand: Optional[str] = None
    amount: float = Field(gt=0, le=_MAX_AMOUNT)
    name: Optional[str] = None
    method: str = "card"
    # Review fix: required so a retry/double-click can be recognized as the
    # SAME request instead of charging twice -- see payments.py::charge() and
    # db/migrations/0007's partial unique index. Caller-generated (e.g. a
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
    last4: Optional[str] = None
    brand: Optional[str] = None
    created_at: Optional[str] = None
