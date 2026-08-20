"""Pydantic models for the Payment Service API."""
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator

from . import redactor

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

    # Bounded to int4, the column's own width. An unbounded int let a caller put
    # a PAN-shaped number here -- `loan_id=4111111111111111` -- and it reached
    # the log line before PostgreSQL ever saw it, so the database rejecting the
    # value afterwards did not help. servicing's `PaymentIn` was bounded in this
    # PR; this is the same bound on the service that actually takes the charge.
    # Reviewed on PR #16.
    loan_id: int = Field(gt=0, le=2_147_483_647)
    # The one free-form string on this model, and it was the one hole left.
    # Length was the only constraint, so `processor_token="4111111111111111"`
    # was accepted at the boundary; it is never persisted (ADR 0008) and it is
    # redacted in the log, but `processor.authorize_charge` posts it verbatim to
    # the processor once `PROCESSOR_API_KEY` is set -- a card number in an
    # outbound request body. Reviewed on PR #51 (PAY-FLOW-001).
    #
    # Refused here as well as in `processor.py` on purpose: this boundary
    # rejects before a `payments` row is written at all, so a card-shaped token
    # produces a 422 rather than a failed payment row someone has to explain.
    processor_token: str = Field(min_length=1, max_length=255)
    last4: str = Field(min_length=4, max_length=4, pattern=r"^\d{4}$")
    # Shape-constrained like `last4` above, and for the same reason: the field
    # named `pan` is forbidden, but an unconstrained string is a channel for the
    # same data. charge() INSERTs this verbatim into the payments row before
    # authorization, so `brand="4111111111111111"` stored a raw card number in
    # the `brand` column -- while the README claimed the INSERT never writes
    # one. Card brands are words. Reviewed on PR #16.
    brand: Optional[str] = Field(default=None, pattern=r"^[A-Za-z][A-Za-z ]{0,19}$")
    amount: float = Field(gt=0, le=_MAX_AMOUNT)
    name: Optional[str] = None
    # An enum, not free text. `method` is persisted verbatim in the same INSERT
    # as `last4`/`brand`, so an unconstrained string was another channel for the
    # data the field named `pan` is rejected for -- `method="4111111111111111"`
    # stored a card number in a column nobody would think to look in. There are
    # exactly two payment methods here. Reviewed on PR #16.
    method: Literal["card", "ach"] = "card"
    # Review fix: required so a retry/double-click can be recognized as the
    # SAME request instead of charging twice -- see payments.py::charge() and
    # db/migrations/0007's partial unique index. Caller-generated (e.g. a
    # UUID minted once per submit attempt, reused on retry).
    #
    # Shape-checked as well as length-checked, for the same reason as `brand` and
    # `method`: it is caller-controlled, it is persisted, and it is formatted into
    # log lines and the 409 body. Redaction covers the log; it does not stop the
    # value being STORED. A key is an opaque correlator, so the charset is the
    # constraint -- a PAN or an SSN cannot be spelled with it, because neither
    # survives without its digits being contiguous.
    idempotency_key: str = Field(min_length=1, max_length=255)

    @field_validator("processor_token")
    @classmethod
    def _token_is_not_sensitive_content(cls, value: str) -> str:
        """Reject a token carrying the card/SSN shapes the redactor knows.

        Same rule and the same shared definition as `idempotency_key` below, for
        a different reason: that field is refused because it is STORED, this one
        because it is TRANSMITTED. Both are caller-controlled free text, and a
        constraint on only the stored one leaves the outbound body open.
        """
        if redactor.looks_sensitive(value):
            raise ValueError(
                "processor_token must be an opaque token issued by the "
                "processor's own tokenization step, not card or personal data -- "
                "it is sent to the processor in a request body"
            )
        return value

    @field_validator("idempotency_key")
    @classmethod
    def _key_is_not_sensitive_content(cls, value: str) -> str:
        """Reject a key that carries the card/SSN shapes the redactor knows.

        Deliberately reuses `redactor`'s own patterns rather than inventing a
        second definition of "looks like a PAN": two definitions drift, and the
        one in the redactor is the one already tested.
        """
        if redactor.looks_sensitive(value):
            raise ValueError(
                "idempotency_key must be an opaque correlator (a UUID, say), not "
                "card or personal data -- it is stored on the payments row"
            )
        return value


class PaymentOut(BaseModel):
    payment_id: Optional[int] = None
    loan_id: int
    # Review fix: an explicit enum, not a free string -- "captured" (the
    # processor confirmed authorization AND the balance is confirmed applied),
    # "pending" (authorization confirmed, balance apply not yet confirmed --
    # a retry with the same idempotency_key keeps reconciling it), or "failed"
    # (the processor declined the authorization -- no balance was ever
    # touched). See app/payments.py::charge() and app/processor.py.
    status: Literal["captured", "pending", "failed"]
    applied_amount: float


class PaymentItem(BaseModel):
    id: int
    amount: float
    method: Optional[str] = None
    last4: Optional[str] = None
    brand: Optional[str] = None
    created_at: Optional[str] = None
