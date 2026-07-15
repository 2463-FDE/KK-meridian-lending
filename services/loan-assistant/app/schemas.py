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


class PolicyAnswer(BaseModel):
    answerable: bool = Field(
        description="Whether the policy corpus actually grounds an answer to the question"
    )
    answer: str = Field(
        description="Grounded answer, or an honest 'not recorded' message if answerable is False"
    )
    source_chunk_id: str | None = Field(
        default=None, description="Which policy chunk grounded the answer, if any"
    )
    source_text: str | None = Field(
        default=None,
        description="The actual retrieved policy excerpt the answer was grounded in, so a "
        "reader can verify it themselves instead of trusting the answer on faith",
    )
