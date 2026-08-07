"""Pydantic response models for the LSS API."""
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class LoanListItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: int
    applicant_name: Optional[str] = None
    principal: float
    # The database column is still `loans.apr`, but what it holds -- and what
    # schedule.amortization() consumes -- is the CONTRACTUAL note rate, not the
    # disclosed federal APR. Exposing it to clients as "apr" was a misleading
    # label on a regulated figure, so the API name says what the value is. The
    # column rename is tracked as D19; until it lands, this alias is the
    # boundary where the legacy name stops.
    note_rate_pct: float = Field(validation_alias="apr", serialization_alias="note_rate_pct")
    term_months: int
    status: Optional[str] = None
    balance: float = 0.0
    past_due: float = 0.0
    opened_at: Optional[str] = None


class LoanDetail(LoanListItem):
    pass


class BalanceOut(BaseModel):
    loan_id: int
    balance: float
    past_due: float = 0.0


class ScheduleRow(BaseModel):
    n: int
    due_date: str
    payment: float
    principal: float
    interest: float
    balance: float


class ScheduleOut(BaseModel):
    loan_id: int
    schedule: list[ScheduleRow]


class PaymentItem(BaseModel):
    id: int
    amount: float
    method: Optional[str] = None
    masked_pan: Optional[str] = None
    created_at: Optional[str] = None


class PaymentsOut(BaseModel):
    loan_id: int
    items: list[PaymentItem]


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int
