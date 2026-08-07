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
    # Two distinct rates, deliberately named so they cannot be confused at any
    # boundary. `note_rate_pct` is the contractual interest rate the payment
    # stream is priced at and the rate servicing amortizes; `apr` is the
    # disclosed federal APR, which additionally carries the prepaid origination
    # fee and is therefore always the larger of the two once a fee exists.
    # Optional because pre-0030 offer rows have no stored note rate.
    note_rate_pct: float | None = None
    apr: float
    finance_charge: float
    # Under Model B `monthly_payment` is the REGULAR payment -- the amount billed
    # in every period except the last. The final period bills `final_payment`,
    # which absorbs the cent residue and is a different number. Presenting only
    # `monthly_payment` and a term told the borrower they would make N identical
    # payments, which is not what the contract says.
    #
    # The name is kept because it is the persisted column name and the field
    # every existing caller reads; renaming it in the API while offers.
    # monthly_payment stays put would put the confusion somewhere else.
    monthly_payment: float
    amount_financed: float
    total_of_payments: float
    # How many periods bill `monthly_payment`, and what the last one bills.
    # Optional because pre-0030 rows have no stored schedule -- and a legacy row
    # must report null here rather than a plausible guess, since a reconstructed
    # final payment presented beside genuine disclosed amounts is
    # indistinguishable from a real one.
    regular_payment_count: int | None = None
    final_payment: float | None = None
    term_months: int | None = None
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
