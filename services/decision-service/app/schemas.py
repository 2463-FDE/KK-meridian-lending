"""Pydantic request/response models for the decision-service API."""
from typing import Optional

from pydantic import BaseModel, Field


class DecisionIn(BaseModel):
    application_id: int
    # PR #6 review (Finding 2): an opaque correlation id, minted by
    # origination-service's own decision_attempts row BEFORE this call is
    # ever made. decision-service does nothing with it except echo it back
    # on DecisionOut -- origination uses the echo to reject a response that
    # doesn't match the attempt it's currently waiting on (see
    # routers/applications.py run_decision). decision-service has no
    # awareness of decision_attempts as a table; this is purely a
    # request/response correlation id, not a persisted foreign key here.
    attempt_id: int
    # PR #6 review (Gap A): origination's idempotency key for this logical
    # decision request. Stable across a retry after an ambiguous timeout,
    # regenerated for a genuinely new decision request -- forwarded to the
    # bureau so a retry recovers the original pull instead of billing a
    # second hard inquiry. See app/bureau.py.
    bureau_request_key: str = Field(min_length=1)
    applicant_id: int
    name: str = Field(min_length=1)
    ssn: str
    requested_amount: float = Field(gt=0)
    term_months: int = Field(ge=12, le=60)
    annual_income: float = Field(ge=0)
    monthly_debt: float = Field(ge=0)
    # The client keeps asking for a smarter "AI" model — that work has not started.
    # When the bureau provides a score it flows through the synchronous chain instead.
    credit_score: Optional[int] = None


class DecisionOut(BaseModel):
    application_id: int
    attempt_id: int
    outcome: str
    score: float
    reason: Optional[str] = None
    reason_codes: list[str] = Field(default_factory=list)
    # PR #6 review (Finding 2): decision-service no longer persists
    # decision_events itself (see graph.py::_node_finalize) -- origination-
    # service is now the one that writes it, atomically with `decisions`,
    # only after winning its own finality recheck. These three fields are
    # everything origination needs to write that row itself; previously
    # they never left decision-service at all.
    bureau_score: Optional[int] = None
    model_version: Optional[str] = None
    top_features: Optional[dict] = None
    # Non-sensitive provider handle for the bureau operation. Persisted by
    # origination (decision_attempts.bureau_reference_id) so a real provider
    # implementation could later look the operation up by reference instead
    # of re-pulling. Never the SSN, never the raw provider response.
    bureau_reference_id: Optional[str] = None
