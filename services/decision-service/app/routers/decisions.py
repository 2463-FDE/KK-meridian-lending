"""Credit decisioning endpoint.

Runs the decisioning chain (async -- see decision.py's module docstring for the
async rework). Persists both the legacy outcome-only `decisions` row and an
append-only `decision_events` row (inputs, model score/version, top features,
reason codes) via decision.decide().
"""
from fastapi import APIRouter

from .. import decision
from ..logging_config import get_logger
from ..schemas import DecisionIn, DecisionOut

log = get_logger("decisions")
router = APIRouter(prefix="/decisions", tags=["decisions"])


@router.post("", response_model=DecisionOut)
async def run_decision(body: DecisionIn):
    payload = body.model_dump()
    # map the request onto the dict the scoring chain expects
    application = {
        "app_id": payload["application_id"],
        "ssn": payload.get("ssn") or "",
        "income": payload.get("annual_income") or 0,
        "requested_amount": payload.get("requested_amount"),
        "term_months": payload.get("term_months"),
    }
    result = await decision.decide(application)
    return DecisionOut(
        application_id=payload["application_id"],
        outcome=result["decision"],
        score=result["score"],
        reason=result.get("adverse_action_reason"),
        reason_codes=result.get("reason_codes") or [],
    )
