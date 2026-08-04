"""Credit decisioning endpoint.

Runs the decisioning chain (async -- see decision.py's module docstring for the
async rework) and persists decision-service's OWN append-only audit trail
(decision_events: inputs, model score/version, top features, reason codes)
via decision.decide().

Architecture fix: this used to also persist the authoritative `decisions`
row (outcome-only, unconditional ON CONFLICT DO UPDATE) -- that write is
gone. This endpoint now only PROPOSES an outcome; origination-service is
the sole writer of `decisions`, under a lock, with a staleness check
against a staff final decision (manual_reviews). See routers/
applications.py::run_decision and app/graph.py::_node_persist.
"""
from fastapi import APIRouter, Header, HTTPException

from .. import config, db, decision
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

    # Security fix: name/ssn/requested_amount/term_months/annual_income used to be
    # trusted straight from the request body and persisted verbatim -- reachable
    # (until the gateway fix) by any caller who could POST an existing
    # application_id with fabricated financials, overwriting the real decision +
    # its audit trail via decide()'s ON CONFLICT DO UPDATE. Only application_id is
    # trusted from the caller now; everything else is loaded from the application's
    # own record, the same one origination-service itself sourced this data from
    # before ever calling here.
    rows = db.query(
        "SELECT a.id, a.amount, a.term_months, a.income, ap.ssn "
        "FROM applications a LEFT JOIN applicants ap ON ap.id = a.applicant_id "
        "WHERE a.id = %s",
        (body.application_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="application not found")
    r = rows[0]
    application = {
        "app_id": r["id"],
        "ssn": r.get("ssn") or "",
        "income": float(r["income"]) if r.get("income") is not None else 0,
        "requested_amount": float(r["amount"]) if r.get("amount") is not None else None,
        "term_months": r.get("term_months"),
    }
    result = await decision.decide(application)
    return DecisionOut(
        application_id=body.application_id,
        outcome=result["decision"],
        score=result["score"],
        reason=result.get("adverse_action_reason"),
        reason_codes=result.get("reason_codes") or [],
    )
