"""Offer / Truth-in-Lending disclosure generation.

The offer build + APR/finance-charge + amortization logic was extracted into
disclosure-service. This router is now a thin pass-through: it calls disclosure-service
over HTTP and maps its response into the OfferOut shape the frontend already expects.

Review fix: make_offer used to have no guard of its own at all -- any outcome
(denied, still-refer, no decision yet) proxied straight through to
disclosure-service, which only ever returned a generic "no approved decision
on record" 422 with no reason, and silently returned 200 with the existing
offer on a repeat call, giving the caller no way to tell "just created" from
"already existed". Guards now live here, with a specific, honest message per
state (see workflow rules: an offer can only ever be created once a
decision's current outcome is APPROVED).
"""
import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .. import clients, config, db, decision_state
from ..logging_config import get_logger
from ..schemas import Disclosure, OfferOut, ScheduleRow

log = get_logger("offers")
router = APIRouter(tags=["offers"])


class OfferIn(BaseModel):
    app_id: int
    principal: float = Field(gt=0, le=50000)
    annual_rate_pct: float = Field(default=7.99, gt=0, le=35)
    term_months: int = Field(default=48, ge=12, le=60)


def _to_offer_out(app_id: int, resp: dict) -> OfferOut:
    """Map a disclosure-service OfferResponse into the LOS OfferOut/Disclosure shape."""
    d = resp.get("disclosure") or {}
    rows = resp.get("schedule") or d.get("schedule") or []
    disclosure = Disclosure(
        apr=d.get("apr", 0), finance_charge=d.get("finance_charge", 0),
        monthly_payment=d.get("monthly_payment", 0),
        amount_financed=d.get("amount_financed", 0),
        total_of_payments=d.get("total_of_payments", 0),
        schedule=[ScheduleRow(**row) for row in rows],
    )
    return OfferOut(app_id=app_id, disclosure=disclosure)


@router.post("/offer", response_model=OfferOut)
def make_offer(body: OfferIn):
    # Same-row check-then-write race as everywhere else in this router set is
    # acceptable here: disclosure-service's own INSERT ... ON CONFLICT
    # (decision_id) DO NOTHING is still the real atomic guard against two
    # concurrent make_offer calls both creating a row; this pre-check exists
    # purely to give a specific, honest message instead of a generic one.
    existing = db.query("SELECT id FROM offers WHERE app_id = %s", (body.app_id,))
    if existing:
        raise HTTPException(
            status_code=409,
            detail="An offer has already been created for this application.",
        )

    dec = db.query("SELECT outcome FROM decisions WHERE app_id = %s", (body.app_id,))
    if not dec:
        raise HTTPException(
            status_code=422,
            detail="An offer cannot be created until the application receives a final approval.",
        )
    outcome = dec[0]["outcome"]
    if outcome == "deny":
        reason = decision_state.get_deny_reason(body.app_id)
        raise HTTPException(
            status_code=422,
            detail=(
                "An offer cannot be created because this application was denied. "
                f"Decision reason: {reason or 'not on record'}."
            ),
        )
    if outcome != "approve":
        # 'refer' or any other non-terminal outcome -- no final approval yet.
        raise HTTPException(
            status_code=422,
            detail="An offer cannot be created until the application receives a final approval.",
        )

    try:
        resp = clients.post(clients.DISCLOSURE_URL, "/offers", {
            "application_id": body.app_id,
            "decision_id": body.app_id,  # decisions.app_id is that table's PK -- 1 decision per app
            "principal": body.principal,
            "term_months": body.term_months,
            "annual_rate": body.annual_rate_pct,
        }, headers={"X-Internal-Token": config.INTERNAL_SERVICE_TOKEN})
    except httpx.HTTPStatusError as exc:
        # disclosure-service rejected the request -- surface ITS OWN already
        # user-safe detail message (not a stack trace) rather than a generic
        # one, so a real reason (e.g. a race that changed the outcome
        # underneath us) is never silently swallowed.
        log.warning("make_offer: disclosure-service rejected app_id=%s: %s", body.app_id, exc)
        try:
            upstream_detail = exc.response.json().get("detail")
        except Exception:  # noqa
            upstream_detail = None
        raise HTTPException(
            status_code=exc.response.status_code,
            detail=upstream_detail or "Could not create the offer for this application.",
        ) from exc
    except Exception as exc:  # noqa
        # Network failure, timeout, etc. -- log the technical detail
        # server-side; the caller never sees anything but a clear, generic
        # message (never an internal error string, a URL, or a stack trace).
        log.error("make_offer failed app_id=%s: %s", body.app_id, exc)
        raise HTTPException(
            status_code=502,
            detail="Could not create the offer -- please try again.",
        ) from exc

    return _to_offer_out(body.app_id, resp)


@router.get("/applications/{app_id}/offer", response_model=OfferOut)
def get_offer(app_id: int):
    resp = clients.get(clients.DISCLOSURE_URL, f"/applications/{app_id}/offer")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="no offer for this application")
    resp.raise_for_status()
    return _to_offer_out(app_id, resp.json())
