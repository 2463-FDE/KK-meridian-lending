"""Pydantic response models for the LSS API."""
from typing import Generic, Optional, TypeVar

from pydantic import BaseModel, ConfigDict, Field

T = TypeVar("T")


class LoanListItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    id: int
    applicant_name: Optional[str] = None
    principal: float
    # `loans.apr` is a legacy column name whose MEANING depends on how the loan
    # was boarded, and that is why this is not a plain rename.
    #
    #   * boarded by the current path -- the value is the contractual note rate,
    #     copied from the offer alongside the stored schedule. Provable, and the
    #     stored schedule is the proof: `schedule_version` is set only when
    #     boarding wrote the contract;
    #   * boarded before the change -- the pre-change acceptance path copied
    #     `offers.apr`, the DISCLOSED APR, into this column. For a contract
    #     priced at 7.99% that is 5.196% (db/migrations/0030, which refuses to
    #     trust the column for exactly this reason).
    #
    # An unconditional alias therefore printed 5.196% to those borrowers as
    # "Interest rate (note rate)" -- a contractual term they were never quoted.
    # Reviewed on PR #10.
    #
    # So the rate is reported only where it is proven, and is null otherwise.
    # Unknown stays unknown: the caller can say "not recorded", which is true,
    # instead of showing a number that is wrong. `note_rate_proven` says which
    # case this is without the client having to infer it from a null.
    note_rate_pct: Optional[float] = None
    note_rate_proven: bool = False
    # The raw column, unrelabelled, for callers that still need it (and so this
    # model does not silently drop data it used to carry).
    apr: Optional[float] = Field(default=None, exclude=True)
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
