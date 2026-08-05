from pydantic import BaseModel, Field
from typing import Literal


class ExternalSignal(BaseModel):
    """A published statistic cited alongside the summary.

    Populated server-side from what the provider actually returned (app/macro.py),
    never from the model's output -- same rule as `applicant_name`. The model is
    shown the figure so it can reason about it, but if it restated the number
    differently the officer still sees the published one.
    """

    source: str = Field(description="Publisher, e.g. 'U.S. Bureau of Labor Statistics'")
    series_id: str = Field(description="Publisher's own series identifier, for verification")
    label: str = Field(description="What the figure measures, in plain English")
    value: float = Field(description="The published value")
    unit: str = Field(description="Unit of the value, e.g. 'percent'")
    period: str = Field(description="The period the figure describes, e.g. 'June 2026'")
    url: str = Field(description="Where a reader can verify it")
    citation: str = Field(description="One-line human-readable citation for display")


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
    external_signals: list[ExternalSignal] = Field(
        default_factory=list,
        description=(
            "Grounded context from outside the application. Empty when the "
            "provider is disabled or unreachable -- the summary is still valid, "
            "it simply has no external context to show."
        ),
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
