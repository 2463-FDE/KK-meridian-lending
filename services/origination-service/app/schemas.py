"""Pydantic request/response models for the LOS API."""
import re
from typing import Generic, Optional, TypeVar

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
    score: int
    adverse_action_reason: Optional[str] = None


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
