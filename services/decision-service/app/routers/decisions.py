"""Credit decisioning endpoint.

Runs the decisioning chain (async -- see decision.py's module docstring for the
async rework). Persists both the legacy outcome-only `decisions` row and an
append-only `decision_events` row (inputs, model score/version, top features,
reason codes) via decision.decide().
"""
from fastapi import APIRouter, Header, HTTPException

from .. import config, decision
from ..logging_config import get_logger
from ..schemas import DecisionIn, DecisionOut

log = get_logger("decisions")
router = APIRouter(prefix="/decisions", tags=["decisions"])


@router.post("", response_model=DecisionOut)
async def run_decision(
    body: DecisionIn,
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
):
    # Defense in depth: the network boundary (no host port -- see
    # docker-compose.yml) is the primary control; this is the fallback in case
    # that boundary is ever mistakenly reopened. An unset config token can
    # never match, so a deploy that forgets to set one fails closed.
    if not config.INTERNAL_SERVICE_TOKEN or x_internal_token != config.INTERNAL_SERVICE_TOKEN:
        raise HTTPException(status_code=401, detail="not authorized")

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
