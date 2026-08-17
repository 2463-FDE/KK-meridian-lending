"""Pydantic response models for the LSS API."""
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class LoanListItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: int
    applicant_name: Optional[str] = None
    principal: float
    # The contractual rate, from `loans.note_rate_pct` -- the column that says
    # what it holds (D19, db/migrations/0038 expand and 0039 contract).
    #
    # **Why this was never a plain rename**, kept because it is the whole reason
    # the work took two migrations. `loans.apr` meant two different things:
    #
    #   * boarded by the current path -- the contractual note rate, copied from
    #     the offer alongside the stored schedule, with `schedule_version` as the
    #     proof that boarding wrote the contract;
    #   * boarded before the change -- the pre-change acceptance path copied
    #     `offers.apr`, the DISCLOSED APR, into that column. For a contract
    #     priced at 7.99% that is 5.196%.
    #
    # An unconditional alias therefore printed 5.196% to those borrowers as
    # "Interest rate (note rate)" -- a contractual term they were never quoted.
    # 0038 recorded the rate only where it could be proven; 0039 refused to drop
    # `apr` until every loan had one. So the value here is now proven by
    # construction, and `note_rate_proven` is retained rather than removed
    # because clients branch on it and because a future source of unproven rates
    # would need it again.
    note_rate_pct: Optional[float] = None
    note_rate_proven: bool = False
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
    # Where these rows came from (db/migrations/0030). "contract" = the payment
    # amounts stored on the loan at boarding. "reconstructed" = solved now from
    # principal, rate and term because no schedule was recorded.
    #
    # Reported rather than inferred, and never conflated: a reconstruction is
    # this generator's opinion of what the terms probably were, not the terms
    # that were agreed. A caller that cannot tell the two apart will present a
    # guess as a contract.
    source: str = "contract"
    # The rounding policy that produced the stored amounts. NULL for a
    # reconstruction, because no policy was recorded to name.
    schedule_version: Optional[str] = None
    # Set only when the stored amounts do not fully amortize the principal.
    # Left absent on a consistent contract so its presence is the signal.
    unamortized_residue: Optional[float] = None
    # Human-readable statement of any caveat above. Present exactly when the
    # rows are not the recorded contract, or when a residue exists.
    note: Optional[str] = None


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
