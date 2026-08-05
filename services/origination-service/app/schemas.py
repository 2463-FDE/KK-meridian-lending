"""Pydantic request/response models for the LOS API."""
import re
from typing import Generic, Literal, Optional, TypeVar

from pydantic import BaseModel, Field, field_validator

T = TypeVar("T")


class ApplicationIn(BaseModel):
    name: str = Field(min_length=1)
    dob: Optional[str] = None
    ssn: Optional[str] = None
    ein: Optional[str] = None
    is_entity: bool = False
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    zip_code: Optional[str] = None
    amount: float = Field(gt=0, le=50000)
    term_months: int = Field(default=36, ge=12, le=60)
    purpose: Optional[str] = None
    income: Optional[float] = Field(default=None, ge=0)
    employer: Optional[str] = None
    job_title: Optional[str] = None
    employment_years: Optional[float] = Field(default=None, ge=0)

    @field_validator("phone")
    @classmethod
    def _validate_phone(cls, v: Optional[str]) -> Optional[str]:
        if v is None or v.strip() == "":
            return v
        digits = re.sub(r"\D", "", v)
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]  # strip a leading US country code
        if len(digits) != 10:
            raise ValueError("phone must be a 10-digit US number")
        return digits

    @field_validator("ssn")
    @classmethod
    def _validate_ssn(cls, v: Optional[str]) -> Optional[str]:
        # Entity applicants use ein instead -- ssn stays optional/empty for them.
        if v is None or v.strip() == "":
            return v
        digits = re.sub(r"\D", "", v)
        if len(digits) != 9:
            raise ValueError("ssn must be a 9-digit US SSN")
        return digits

    @field_validator("zip_code")
    @classmethod
    def _validate_zip(cls, v: Optional[str]) -> Optional[str]:
        # W8: fair-lending ZIP-level check needs a real, consistent ZIP --
        # not "whatever the applicant typed" buried inside free-text address.
        if v is None or v.strip() == "":
            return v
        digits = re.sub(r"\D", "", v)
        if len(digits) == 9:
            digits = digits[:5]  # ZIP+4 -- the base 5-digit ZIP is all the fairness check needs
        if len(digits) != 5:
            raise ValueError("zip_code must be a 5-digit US ZIP (optionally ZIP+4)")
        return digits


class KycOut(BaseModel):
    name_verified: bool
    dob_verified: bool
    address_verified: bool
    ssn_verified: bool


class ApplicationCreated(BaseModel):
    app_id: int
    status: str
    kyc: KycOut
    # Review fix: proves ownership for the first (anonymous, no-account)
    # decision call on this application -- see routers/applications.py
    # run_decision. Always present; minted on every submission.
    access_token: str


class ApplicantOut(BaseModel):
    id: int
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    address: Optional[str] = None
    is_entity: bool = False


class ApplicationListItem(BaseModel):
    id: int
    applicant_name: Optional[str] = None
    amount: float
    term_months: int
    purpose: Optional[str] = None
    status: Optional[str] = None
    created_at: Optional[str] = None


class DecisionOut(BaseModel):
    app_id: int
    decision: str
    # Optional: a manual staff review (see ReviewIn/review_application below)
    # produces no model score at all -- there's no model run to report one
    # from, only a human's approve/deny call.
    score: Optional[int] = None
    adverse_action_reason: Optional[str] = None
    # Review fix: minted only when decision == "approve" (see run_decision) --
    # the one-time proof of ownership an anonymous, no-account borrower needs
    # to accept their own offer. See routers/applications.py accept_offer.
    accept_token: Optional[str] = None


class ReviewIn(BaseModel):
    # Feature: lets staff resolve a "refer" decision (policies/underwriting_
    # guidelines.md's manual-review band, score 600-659 or DTI 43-50%) into a
    # real approve/deny -- see routers/applications.py::review_application.
    # Scoped to approve/deny only for now, no counteroffer.
    outcome: Literal["approve", "deny"]
    reason: str = Field(min_length=1, max_length=2000)


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
    # Idempotency fix: a repeat/racing POST /offer for an application that
    # already has one (e.g. auto-generated the instant the decision came
    # back approve) now returns that SAME offer instead of a 409 -- this
    # tells the caller which happened without parsing anything.
    created: bool = True


class ApplicationDetail(BaseModel):
    id: int
    applicant: Optional[ApplicantOut] = None
    amount: float
    term_months: int
    purpose: Optional[str] = None
    status: Optional[str] = None
    employer: Optional[str] = None
    job_title: Optional[str] = None
    kyc: Optional[KycOut] = None
    decision: Optional[str] = None
    offer: Optional[Disclosure] = None
    # Review fix: once staff decides (review_application), that decision is
    # final -- the frontend needs to know this to disable the Approve/Deny
    # controls, not just rely on the backend's own 409 after a doomed retry.
    decision_final: bool = False
    # Bug fix: without these, the finalized-decision panel had nothing real
    # to show -- staff could only see the original reason/who/when by
    # deliberately attempting (and being blocked by) a second decision.
    decision_reason: Optional[str] = None
    decision_by: Optional[str] = None
    decision_at: Optional[str] = None


# income/employment_years are underwriting inputs, not borrower-facing status data.
# GET /applications/{id} is reachable anonymously (see gateway /los/* passthrough),
# so these live on a separate staff-only response instead of ApplicationDetail.
class ApplicationFinancials(BaseModel):
    income: Optional[float] = None
    employment_years: Optional[float] = None


class Page(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int
