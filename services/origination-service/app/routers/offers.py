"""Offer / Truth-in-Lending disclosure generation.

The offer build + APR/finance-charge + amortization logic was extracted into
disclosure-service. This router is now a thin pass-through: it calls disclosure-service
over HTTP and maps its response into the OfferOut shape the frontend already expects.

Review fix: make_offer used to have no guard of its own at all -- any outcome
(denied, still-refer, no decision yet) proxied straight through to
disclosure-service, which only ever returned a generic "no approved decision
on record" 422 with no reason. Guards live here, with a specific, honest
message per state (see workflow rules: an offer can only ever be created
once a decision's current outcome is APPROVED).

Bug fix (borrower-workflow audit): this used to ALSO reject with a 409 the
moment ANY offer already existed for the application -- including the
completely normal case where run_decision's/review_application's own
best-effort auto-generation had already created one (see disclosure_graph.
auto_generate_offer). disclosure-service's own INSERT ... ON CONFLICT
(decision_id) DO NOTHING + read-back was ALREADY idempotent and safe for
this; the 409 here was a redundant, incorrectly-blocking guard on top of it
that broke the public /apply page's own borrower self-service flow (a
retried/first call always found an offer already there and always 409'd).
Removed -- make_offer is now itself idempotent, distinguishing "just
created" (created=True) from "already existed" (created=False) via the
response body, never a 409, for the normal case. A genuine conflict
(application no longer approved, already boarded) still gets a specific,
structured error -- see _conflict() below.
"""
import httpx
from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from .. import clients, config, db, decision_state
from ..logging_config import get_logger
from ..schemas import Disclosure, OfferOut, ScheduleRow
from .applications import _is_staff

log = get_logger("offers")
router = APIRouter(tags=["offers"])


class OfferIn(BaseModel):
    app_id: int
    principal: float = Field(gt=0, le=50000)
    annual_rate_pct: float = Field(default=7.99, gt=0, le=35)
    term_months: int = Field(default=48, ge=12, le=60)


def _conflict(status_code: int, code: str, message: str) -> HTTPException:
    """A structured, machine-readable conflict -- callers (the frontend)
    switch on `code`, never on parsing `message`'s human text. `message`
    stays present for anything that still just logs/displays it directly
    (existing convention throughout this codebase)."""
    return HTTPException(status_code=status_code, detail={"code": code, "message": message})


def _to_offer_out(app_id: int, resp: dict) -> OfferOut:
    """Map a disclosure-service OfferResponse into the LOS OfferOut/Disclosure shape."""
    d = resp.get("disclosure") or {}
    rows = resp.get("schedule") or d.get("schedule") or []
    disclosure = Disclosure(
        apr=d.get("apr", 0), finance_charge=d.get("finance_charge", 0),
        monthly_payment=d.get("monthly_payment", 0),
        amount_financed=d.get("amount_financed", 0),
        total_of_payments=d.get("total_of_payments", 0),
        # Forwarded, not defaulted. `.get(x, 0)` is right for the four amounts
        # -- an offer that reached this point has them, and Gap F refuses the row
        # otherwise -- but a MISSING final payment is meaningfully different from
        # a zero one, so these carry None through. The borrower screen shows a
        # single monthly figure when they are absent rather than describing a
        # final payment of $0.00.
        regular_payment_count=d.get("regular_payment_count"),
        final_payment=d.get("final_payment"),
        term_months=d.get("term_months"),
        schedule=[ScheduleRow(**row) for row in rows],
    )
    return OfferOut(app_id=app_id, disclosure=disclosure, created=resp.get("created", True))


@router.post("/offer", response_model=OfferOut)
def make_offer(
    body: OfferIn,
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    x_offer_accept_token: str | None = Header(default=None, alias="X-Offer-Accept-Token"),
):
    # Security fix (PR #6 review): this had NO auth check at all -- only
    # app_id (attacker-supplied, sequential/guessable) gated everything, so
    # a stranger could retrieve another applicant's real offer terms (this
    # route is idempotent -- ON CONFLICT DO NOTHING + read-back -- so a
    # bogus principal/rate/term in the request body doesn't matter, the
    # EXISTING offer for that decision is returned regardless), or learn a
    # denied applicant's specific denial reason via the 422 below. Same gate
    # as GET /applications/{app_id}/offer: staff, or the accept_token
    # (hash-match only) minted onto this application when it was approved.
    # Checked FIRST, before any state is revealed -- a non-staff/non-owner
    # caller gets the same 403 whether the app doesn't exist, isn't
    # approved, or was denied.
    app_rows = db.query(
        "SELECT status, accept_token_hash FROM applications WHERE id = %s", (body.app_id,)
    )
    if not _is_staff(x_user_role, x_internal_token):
        if not app_rows or not decision_state.accept_token_hash_matches(
            app_rows[0].get("accept_token_hash"), x_offer_accept_token
        ):
            raise HTTPException(status_code=403, detail="not authorized to create or view this offer")
    if not app_rows:
        raise HTTPException(status_code=404, detail="application not found")
    # Bug fix: an already-boarded application must never mint/return a
    # "fresh" offer flow -- boarding only ever happens after acceptance, so
    # reaching this with status == 'funded' means either a stale client
    # retry or the legacy direct-/board path (no offer ever existed for
    # that one). Either way, this is a real, structured conflict, not the
    # normal already-exists case handled below.
    if app_rows[0]["status"] == "funded":
        raise _conflict(
            409, "APPLICATION_ALREADY_BOARDED",
            "This application has already been boarded; an offer can no longer be created or changed.",
        )

    dec = db.query("SELECT outcome FROM decisions WHERE app_id = %s", (body.app_id,))
    if not dec:
        raise _conflict(
            422, "APPLICATION_NOT_APPROVED",
            "An offer cannot be created until the application receives a final approval.",
        )
    outcome = dec[0]["outcome"]
    if outcome == "deny":
        reason = decision_state.get_deny_reason(body.app_id)
        raise _conflict(
            422, "APPLICATION_NOT_APPROVED",
            "An offer cannot be created because this application was denied. "
            f"Decision reason: {reason or 'not on record'}.",
        )
    if outcome != "approve":
        # 'refer' or any other non-terminal outcome -- no final approval yet.
        raise _conflict(
            422, "APPLICATION_NOT_APPROVED",
            "An offer cannot be created until the application receives a final approval.",
        )

    # Idempotency fix: no more pre-check-then-409 here -- disclosure-service's
    # own INSERT ... ON CONFLICT (decision_id) DO NOTHING + read-back is the
    # real, database-enforced "exactly one offer per decision" guarantee
    # (offers.decision_id / offers.app_id are both UNIQUE). This call is
    # safe to make whether or not an offer already exists: it returns the
    # SAME offer either way, with `created` telling the two cases apart.
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
        # server-side with a correlation id (app_id); the caller never sees
        # anything but a clear, generic, recoverable message.
        log.error("make_offer failed app_id=%s: %s", body.app_id, exc)
        raise _conflict(
            502, "OFFER_SERVICE_UNAVAILABLE",
            "Could not create the offer -- please try again.",
        ) from exc

    return _to_offer_out(body.app_id, resp)


@router.get("/applications/{app_id}/offer", response_model=OfferOut)
def get_offer(
    app_id: int,
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    x_offer_accept_token: str | None = Header(default=None, alias="X-Offer-Accept-Token"),
):
    # Security fix (borrower-workflow audit): this had NO ownership check at
    # all -- app_id is a sequential, guessable integer, so anyone could read
    # a STRANGER's loan amount/APR/payment schedule. Staff or the SAME
    # accept_token minted onto this application on approval (the borrower's
    # own browser already holds it, from the decision response) is now
    # required -- same credential accept_offer already uses, just a lighter
    # check: hash-match only, no expiry/consumed-state gate, since viewing
    # is read-only and a borrower re-viewing their own already-accepted
    # offer (token consumed) or an offer whose token has since expired is
    # not a security concern the way accepting/boarding again would be.
    #
    # Security fix (follow-up audit): the token used to travel as a
    # ?accept_token=... query parameter -- proven, with a live canary
    # value, to leak into this service's own uvicorn access log and the
    # gateway's access + outbound httpx logs (neither disables access
    # logging). Query-parameter token auth is removed entirely, no
    # backward-compatible fallback -- X-Offer-Accept-Token (a header, never
    # part of a URL, never in a default access-log line) is the only way
    # this credential travels now, for both this route and accept_offer
    # below.
    # Security fix (follow-up, PR #6 review): the branch below used to raise
    # a distinct 404 ("application not found") vs 403 ("not authorized") --
    # an existence oracle letting a caller with no credential at all
    # distinguish "no such app_id" from "exists but not mine" by enumerating
    # ids. Both now collapse to the same generic 403 for a non-staff caller.
    if not _is_staff(x_user_role, x_internal_token):
        rows = db.query(
            "SELECT accept_token_hash FROM applications WHERE id = %s",
            (app_id,),
        )
        if not rows or not decision_state.accept_token_hash_matches(
            rows[0].get("accept_token_hash"), x_offer_accept_token
        ):
            # Never echo the caller's supplied token back in the error --
            # only ever a fixed, generic message, regardless of whether
            # app_id exists at all.
            raise HTTPException(status_code=403, detail="not authorized to view this offer")

    resp = clients.get(clients.DISCLOSURE_URL, f"/applications/{app_id}/offer")
    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="no offer for this application")
    resp.raise_for_status()
    return _to_offer_out(app_id, resp.json())
