from pydantic import BaseModel, Field
from typing import Literal


class LoanSummary(BaseModel):
    applicant_name: str = Field(description="Full name of applicant")
    loan_amount: float = Field(description="Requested loan amount in USD")
    term_months: int = Field(description="Loan term in months")
    purpose: str = Field(description="Stated purpose of the loan")
    risk_tier: Literal["low", "medium", "high", "decline"] = Field(
        description="Officer-facing risk tier based on income, amount, and employment"
    )
    summary: str = Field(
        description="2-3 sentence plain-English summary for the loan officer. No PAN, CVV, or SSN."
    )
    flags: list[str] = Field(
        default_factory=list,
        description="List of specific concerns the officer should review (no raw PII)",
    )
