"""Pydantic request/response models for the disclosure API."""
from pydantic import BaseModel, Field


class OfferIn(BaseModel):
    application_id: int
    decision_id: int | None = None  # W4: which decision this offer follows, if any
    principal: float = Field(gt=0, le=50000)
    term_months: int = Field(default=48, ge=12, le=60)
    annual_rate: float = Field(default=7.99, gt=0, le=35)


class ScheduleRow(BaseModel):
    n: int
    due_date: str
    payment: float
    principal: float
    interest: float
    balance: float


class Disclosure(BaseModel):
    apr: float
    finance_charge: float
    monthly_payment: float
    amount_financed: float
    total_of_payments: float
    schedule: list[ScheduleRow] = []


class OfferOut(BaseModel):
    app_id: int
    disclosure: Disclosure


class OfferResponse(BaseModel):
    offer_id: int
    application_id: int
    decision_id: int | None = None
    fee_pct_used: float | None = None
    apr: float
    finance_charge: float
    monthly_payment: float
    total_of_payments: float
    disclosure: Disclosure
    schedule: list[ScheduleRow] = []
    # Idempotency fix: create_offer() is safe to call again for an
    # application that already has one (ON CONFLICT DO NOTHING, then read
    # back the original row) -- callers need to tell "just created" from
    # "already existed" apart without parsing anything, since the latter is
    # the normal case whenever server-side auto-generation already ran.
    created: bool = True
    # True only when this call repaired an existing INCOMPLETE offer in place
    # (see create_offer). Never true for a normal create or a normal retry.
    repaired: bool = False
