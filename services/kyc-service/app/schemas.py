"""Pydantic request/response models for the KYC API."""
from typing import Optional

from pydantic import BaseModel


class CipCheckIn(BaseModel):
    """What origination can legitimately send, not what a complete applicant has.

    Review finding: dob, ssn and address were required strings here while
    origination's ApplicationIn has all three Optional. An entity applicant has
    no DOB or SSN *by design* -- this service's own CIP logic says so and clears
    them on name and address alone -- so every entity application produced a 422,
    no kyc_checks row, an intake that still reported "submitted", and a decision
    gate that then refused the application. The two schemas disagreed about the
    same domain, and the stricter one was wrong.

    run_cip already treats each field as possibly absent (`bool(applicant.get(...))`),
    so accepting None changes no verification behaviour: a missing field verifies
    as False, which is exactly what it means.
    """
    application_id: int
    applicant_id: int
    name: Optional[str] = None
    dob: Optional[str] = None
    ssn: Optional[str] = None
    address: Optional[str] = None
    entity_type: Optional[str] = None


class CipCheckOut(BaseModel):
    check_id: int
    application_id: int
    status: str          # "pass" | "fail"
    cip_passed: bool
    # CIP only. These two are hardcoded false to keep the gap visible (debt D11):
    # the service performs NO sanctions/OFAC screening and captures NO beneficial owner.
    sanctions_screened: bool = False
    ubo_captured: bool = False
    notes: str
