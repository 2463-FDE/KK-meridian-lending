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

    # Where this payment actually went, read from the ledger entries it wrote
    # (`db/migrations/0035`, one row per component keyed on
    # `(payment_id, component)`). NOT recomputed: re-running the waterfall at
    # read time would produce a second opinion about a movement that already
    # happened, and the two could disagree after a waiver, an adjustment or a
    # schedule correction. The ledger is what moved the balance, so the ledger
    # is what this reports.
    #
    # `None` -- not 0.00 -- when the payment has no ledger entries at all. A
    # zero would assert "nothing went to interest"; the truth for a legacy row
    # applied before the ledger existed is "unknown", and those are different
    # answers to give a borrower.
    applied_to_fees: Optional[float] = None
    applied_to_interest: Optional[float] = None
    applied_to_principal: Optional[float] = None


class PaymentsOut(BaseModel):
    loan_id: int
    items: list[PaymentItem]


class ActivityItem(BaseModel):
    """One authoritative movement that changed this account.

    Distinct from `PaymentItem`, which answers a different question. Payment
    history asks "what payments did I make, and where did each go"; activity asks
    "what movements changed this account" -- and the answers differ, because an
    approved adjustment and a fee waiver change the account without being
    payments, while a declined proposal changes nothing at all and appears in
    neither.
    """

    #: Stable within a loan, and derived from authoritative identity: the payment
    #: id when there is one, the ledger row id otherwise. Never a hash of amount
    #: and time -- two legitimate payments can share both.
    id: str
    occurred_at: Optional[str] = None
    #: A truthful category, mapped server-side from `ledger_entries.entry_type`.
    #: Never the raw type: `legacy_direct_write` is an implementation name for a
    #: balance change captured by a database trigger, and putting it in front of a
    #: borrower would be both meaningless and alarming.
    category: str
    description: str
    #: The SIGNED effect on what the borrower owes, in the ledger's own
    #: convention: negative reduces the amount owed, positive increases it. Not
    #: flipped, unlike payment history -- the sign IS the information here, and it
    #: is the same convention the adjustment form uses (+450 owes more).
    amount: float
    #: The same movement split by component, for the payments that have a split.
    #: `interest` appears here even though it projects to no balance column: it is
    #: money the borrower paid, and omitting it would make a payment's parts fail
    #: to sum to its whole.
    components: dict[str, float] = {}
    #: Present only for movements that came from a payment. This is what groups
    #: several ledger rows into one movement, and what a caller can join against
    #: payment history.
    payment_id: Optional[int] = None
    #: How well this movement's origin is known. `processor` is a real captured
    #: payment; `recorded` is an entry written by a service with full provenance;
    #: `limited` is a pre-ledger or trigger-captured row whose actor and reason
    #: were never recorded. A reader must be able to tell the difference.
    provenance: str


class ActivityOut(BaseModel):
    loan_id: int
    items: list[ActivityItem]
    #: What this list is and is not, in the payload rather than only in the UI.
    note: str


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int
