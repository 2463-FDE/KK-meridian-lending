"""Application intake, listing, detail, decisioning, and acceptance/boarding."""
import secrets

import psycopg2.errors
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import clients, config, db, decision_state, disclosure_graph, fair_lending, intake, kg, models
from ..database import get_session
from ..logging_config import get_logger
from ..schemas import (
    ApplicationCreated,
    ApplicationDetail,
    ApplicationFinancials,
    ApplicationIn,
    ApplicationListItem,
    ApplicantOut,
    DecisionOut,
    Disclosure,
    KycOut,
    Page,
    ReviewIn,
)

log = get_logger("applications")
router = APIRouter(prefix="/applications", tags=["applications"])

# Roles allowed to see underwriting-sensitive fields (income, employment_years).
# Mirrors the staff role set the gateway already enforces for /assistant/*.
_STAFF_ROLES = {"csr", "underwriter", "admin"}

# Bug fix: applications.status used to only ever move 'submitted' -> 'funded'
# (see accept_offer below) -- run_decision never wrote the decision outcome
# back onto it at all. The underwriting console's status filter/KPIs
# (frontend/app/underwriting/page.tsx) check for exactly these values, but
# nothing in real request flow ever produced them -- only the synthetic bulk
# seed data (db/init/003_seed_bulk.sql) faked a status column, bypassing the
# app entirely. Every real, live-decisioned application was invisible to that
# filter/KPI.
_DECISION_STATUS = {"approve": "approved", "refer": "in_review", "deny": "denied"}

# NOTE: the gateway's /los/{path:path} route (gateway/app/main.py) proxies to this
# router with NO auth check — an applicant can check their own status without an
# account, so anyone who guesses an app_id can hit any GET route here anonymously.
# Before adding a new field to ApplicationDetail, ApplicationListItem, or any other
# response model returned by a route in this file, ask:
#   1. Would this be sensitive if read by someone who only knows the app_id?
#      (income, SSN, DOB, credit score, decision reasoning, etc. -> yes)
#   2. If yes, put it on a separate endpoint gated by _STAFF_ROLES (see
#      get_application_financials below), not on the public response.


def _is_staff(x_user_role: str | None, x_internal_token: str | None) -> bool:
    """Review fix: every staff-gated route below used to trust X-User-Role
    alone. docker-compose.yml no longer publishes this service's host port,
    but that's network topology, not an application-level check -- if the
    port were ever reopened (or this service reached some other way inside
    the compose network), a direct caller could set X-User-Role: admin itself
    with nothing to verify the claim, and fund/read a guessed app_id with no
    real staff session behind it. The gateway now forwards X-Internal-Token
    (the same shared secret already used for the decision-service call below)
    on every /los/* proxy; a direct caller doesn't know that secret, so it can
    claim any role it wants but still never pass this check.
    """
    if x_user_role not in _STAFF_ROLES:
        return False
    return bool(config.INTERNAL_SERVICE_TOKEN) and x_internal_token == config.INTERNAL_SERVICE_TOKEN


def _require_staff(x_user_role: str | None, x_internal_token: str | None) -> None:
    if not _is_staff(x_user_role, x_internal_token):
        raise HTTPException(status_code=403, detail="staff only")


@router.post("", response_model=ApplicationCreated)
def submit_application(body: ApplicationIn):
    payload = body.model_dump()
    # creates applicant+application rows, logs full PII (D5 — KEEP)
    app_id, access_token = intake.create_application(payload)
    # Resolve applicant_id the same way the old in-process path did.
    applicant_id = None
    try:
        applicant_rows = db.query(
            "SELECT applicant_id FROM applications WHERE id = %s", (app_id,)
        )
        applicant_id = applicant_rows[0]["applicant_id"] if applicant_rows else None
    except Exception as e:  # noqa
        log.warning("could not resolve applicant_id: %s", e)

    # CIP/KYC moved to kyc-service. It persists its own kyc_checks row (so no INSERT here).
    # Default to all-false; a kyc-service hiccup must not 500 the intake (resilience kept).
    cip = {"name_verified": False, "dob_verified": False,
           "address_verified": False, "ssn_verified": False}
    is_entity = bool(payload.get("is_entity"))
    try:
        resp = clients.post(clients.KYC_URL, "/kyc/check", {
            "application_id": app_id,
            "applicant_id": applicant_id,
            "name": payload.get("name"),
            "dob": payload.get("dob"),
            "ssn": payload.get("ssn"),
            "address": payload.get("address"),
            "entity_type": "llc" if is_entity else None,
        })
        passed = bool(resp.get("cip_passed"))
        # Map kyc-service cip_passed -> the four KycOut booleans the frontend expects.
        # CIP verifies name/dob/address/ssn that were provided; entity applicants have no
        # dob/ssn so those stay false even on a pass (mirrors the old in-process stub).
        cip = {
            "name_verified": passed,
            "dob_verified": passed and not is_entity,
            "address_verified": passed,
            "ssn_verified": passed and not is_entity,
        }
    except Exception as e:  # noqa
        log.warning("kyc-service call failed: %s", e)
    return {"app_id": app_id, "status": "submitted", "kyc": KycOut(**cip), "access_token": access_token}


@router.get("", response_model=Page[ApplicationListItem])
def list_applications(
    session: Session = Depends(get_session),
    status: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    stmt = select(models.Application, models.Applicant.name).join(
        models.Applicant, models.Application.applicant_id == models.Applicant.id, isouter=True
    )
    count_stmt = select(func.count(models.Application.id))
    if status:
        stmt = stmt.where(models.Application.status == status)
        count_stmt = count_stmt.where(models.Application.status == status)
    total = session.scalar(count_stmt) or 0
    stmt = stmt.order_by(models.Application.id.desc()).limit(limit).offset(offset)
    items = [
        ApplicationListItem(
            id=a.id, applicant_name=name, amount=a.amount, term_months=a.term_months,
            purpose=a.purpose, status=a.status,
            created_at=a.created_at.isoformat() if a.created_at else None,
        )
        for a, name in session.execute(stmt).all()
    ]
    return Page(items=items, total=total, limit=limit, offset=offset)


@router.get("/fair-lending/zip-analysis")
def get_zip_disparate_impact_report(
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
):
    """W8: ZIP-level disparate-impact screen (fair_lending.py). Registered
    before /{app_id} -- a literal path segment must be matched ahead of a
    catch-all path parameter, or "fair-lending" would be parsed as an app_id
    and 422 on the int conversion instead of ever reaching this route.
    Staff only: approval-rate breakdowns are underwriting-sensitive, same bar
    as get_application_financials below.
    """
    _require_staff(x_user_role, x_internal_token)
    return fair_lending.zip_disparate_impact_report()


@router.get("/{app_id}", response_model=ApplicationDetail)
def get_application(app_id: int, session: Session = Depends(get_session)):
    a = session.get(models.Application, app_id)
    if not a:
        raise HTTPException(status_code=404, detail="application not found")
    applicant = a.applicant
    kyc_row = session.scalar(
        select(models.KycCheck).where(models.KycCheck.applicant_id == a.applicant_id)
        .order_by(models.KycCheck.id.desc())
    ) if a.applicant_id else None
    dec = session.get(models.Decision, app_id)
    offer = session.scalar(
        select(models.Offer).where(models.Offer.app_id == app_id).order_by(models.Offer.id.desc())
    )
    # Review fix: the frontend needs to know a staff decision is already
    # final (manual_reviews.app_id is UNIQUE, db/migrations/0020) so it can
    # disable Approve/Deny up front, not just discover it via a 409 on
    # submit. Bug fix: exposing only a bool left staff with no way to see
    # the ORIGINAL decision's reason/who/when without deliberately attempting
    # (and being blocked by) a second decision -- fetch the actual row. No
    # ORM model for manual_reviews exists -- a raw query is simpler than
    # adding one for this single read.
    mr = decision_state.get_manual_review(app_id)
    return ApplicationDetail(
        id=a.id,
        applicant=ApplicantOut(
            id=applicant.id, name=applicant.name, email=applicant.email,
            phone=applicant.phone, address=applicant.address, is_entity=applicant.is_entity,
        ) if applicant else None,
        amount=a.amount, term_months=a.term_months, purpose=a.purpose, status=a.status,
        employer=a.employer, job_title=a.job_title,
        kyc=KycOut(
            name_verified=bool(kyc_row.name_verified), dob_verified=bool(kyc_row.dob_verified),
            address_verified=bool(kyc_row.address_verified), ssn_verified=bool(kyc_row.ssn_verified),
        ) if kyc_row else None,
        decision=dec.outcome if dec else None,
        decision_final=mr is not None,
        decision_reason=mr["reason"] if mr else None,
        decision_by=(mr["reviewer_name"] or mr["reviewer_role"]) if mr else None,
        decision_at=mr["reviewed_at"].isoformat() if mr and hasattr(mr["reviewed_at"], "isoformat") else None,
        offer=Disclosure(
            apr=offer.apr or 0, finance_charge=offer.finance_charge or 0,
            monthly_payment=offer.monthly_payment or 0, amount_financed=offer.amount_financed or 0,
            total_of_payments=offer.total_of_payments or 0,
        ) if offer else None,
    )


@router.get("/{app_id}/financials", response_model=ApplicationFinancials)
def get_application_financials(
    app_id: int,
    session: Session = Depends(get_session),
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
):
    # Staff only: income/employment_years are underwriting inputs, not borrower
    # status data. The gateway forwards X-User-Role for authenticated sessions
    # (see gateway/app/main.py _proxy); anonymous /los/* callers send none.
    _require_staff(x_user_role, x_internal_token)
    a = session.get(models.Application, app_id)
    if not a:
        raise HTTPException(status_code=404, detail="application not found")
    return ApplicationFinancials(income=a.income, employment_years=a.employment_years)


class DecisionIn(BaseModel):
    # Review fix: proof of ownership for the FIRST decision call -- minted at
    # submission (ApplicationCreated.access_token) and held only by the
    # borrower's own browser for this session. Optional so a staff-session
    # call (the underwriting console's own "Run decision" button) needs none.
    access_token: str | None = None


@router.post("/{app_id}/decision", response_model=DecisionOut)
def run_decision(
    app_id: int,
    body: DecisionIn = DecisionIn(),
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
):
    # Security fix: this route has no session of its own -- the gateway's /los/*
    # proxy forwards it anonymously on purpose, since a freshly-submitted
    # applicant has no account yet and this is how they get their first
    # decision (frontend/app/apply/page.tsx's "Get decision" button).
    #
    # Once a decision exists, a rerun requires a staff session (the
    # underwriting console's own "Run decision" button already sends one) --
    # same _STAFF_ROLES gate as get_application_financials above -- since
    # otherwise anyone who guesses an app_id could rerun decisioning on a
    # stranger's already-decided application, triggering a real bureau pull
    # and overwriting their decision row via decision-service's own
    # ON CONFLICT (app_id) DO UPDATE (graph.py).
    #
    # Security fix (review): the rerun guard above used to be the ONLY check
    # -- the very FIRST decision call was wide open, so anyone who guessed an
    # app_id could trigger a real bureau pull (a credit check) using a
    # stranger's stored SSN. A first call now also requires either a staff
    # session or the access_token minted onto this application at submission.
    rows = db.query(
        "SELECT a.id, a.applicant_id, a.amount, a.term_months, a.income, a.access_token, "
        "a.status, ap.name, ap.ssn "
        "FROM applications a LEFT JOIN applicants ap ON ap.id = a.applicant_id WHERE a.id = %s",
        (app_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="application not found")
    r = rows[0]

    existing = db.query("SELECT app_id FROM decisions WHERE app_id = %s", (app_id,))
    if existing:
        if not _is_staff(x_user_role, x_internal_token):
            raise HTTPException(status_code=403, detail="staff only to rerun a decision")
        # Bug fix: reruns had no guard beyond staff-only -- since scoring is
        # deterministic (same SSN/income -> same score), rerunning after the
        # application was already funded silently reset its recorded decision
        # back to the automated outcome (e.g. "refer") while the loan sat
        # funded on top of it -- a real data-integrity break. Rerunning after
        # a manual review (see review_application/manual_reviews) is just as
        # bad: it silently overwrote a staff decision with a fresh automated
        # one, and since that reset the outcome back to "refer" it made the
        # application eligible for manual review AGAIN, letting the same app
        # get reviewed and reversed indefinitely.
        if r.get("status") == "funded":
            raise HTTPException(
                status_code=422,
                detail="cannot rerun a decision on an already-funded application",
            )
        # Review fix: this used to be a generic 422 message. A final manual
        # decision existing at all is a genuine conflict with the request to
        # rerun (not a client input error, and not silently proceeding) --
        # 409 is the correct status, and the message now states the actual
        # outcome/reason/who/when instead of just "resolved by staff".
        manual = decision_state.get_manual_review(app_id)
        if manual:
            raise HTTPException(status_code=409, detail=decision_state.format_rerun_blocked_message(manual))
    else:
        is_owner = bool(body.access_token) and bool(r.get("access_token")) and body.access_token == r["access_token"]
        if not _is_staff(x_user_role, x_internal_token) and not is_owner:
            raise HTTPException(status_code=403, detail="not authorized to request a decision for this application")

    # Architecture fix (single-writer race closure): decision-service now
    # only COMPUTES a proposed outcome (and records its own append-only
    # decision_events audit row) -- it no longer writes the authoritative
    # `decisions` row itself (see graph.py::_node_persist). Called
    # deliberately BEFORE opening any transaction below: nothing about
    # computing the proposal needs to be atomic, and holding a lock across
    # a network call this slow (a simulated bureau pull) would be its own
    # problem even before considering deadlock risk.
    resp = clients.post(clients.DECISION_URL, "/decisions", {
        "application_id": app_id,
        "applicant_id": r.get("applicant_id"),
        "name": r.get("name"),
        "ssn": r.get("ssn") or "",
        "requested_amount": float(r.get("amount")),
        "term_months": r.get("term_months"),
        "annual_income": float(r.get("income") or 0),
        "monthly_debt": 0,            # not captured in the LOS today
        "credit_score": None,         # pulled downstream by decision-service
    }, headers={"X-Internal-Token": config.INTERNAL_SERVICE_TOKEN})
    outcome = resp["outcome"]

    # Audit fix: origination-service is now the SOLE writer of `decisions`.
    # This transaction locks the same coordination row (applications, FOR
    # UPDATE) that review_application's own transaction locks before
    # touching manual_reviews/decisions -- both endpoints contend for the
    # same lock before writing either table, so whichever commits first is
    # genuinely final, not "probably fine because scoring is deterministic".
    # No external call happens while this lock is held (decision-service's
    # call already finished above), so there is no deadlock risk here,
    # unlike the previous detect-and-abort approach this replaces.
    accept_token = None
    with db.transaction() as cur:
        cur.execute("SELECT status FROM applications WHERE id = %s FOR UPDATE", (app_id,))
        locked = cur.fetchall()
        if not locked:
            raise HTTPException(status_code=404, detail="application not found")
        if locked[0]["status"] == "funded":
            raise HTTPException(
                status_code=422,
                detail="cannot rerun a decision on an already-funded application",
            )
        cur.execute(
            "SELECT outcome, reason, reviewer_name, reviewer_role, reviewed_at "
            "FROM manual_reviews WHERE app_id = %s",
            (app_id,),
        )
        manual_locked = cur.fetchall()
        if manual_locked:
            raise HTTPException(
                status_code=409,
                detail=decision_state.format_rerun_blocked_message(manual_locked[0]),
            )

        cur.execute(
            "INSERT INTO decisions (app_id, outcome) VALUES (%s, %s) "
            "ON CONFLICT (app_id) DO UPDATE SET outcome = EXCLUDED.outcome",
            (app_id, outcome),
        )
        # Bug fix: reflect the outcome onto applications.status -- guarded so a
        # staff rerun on an already-funded application can never regress a
        # funded row backward (redundant with the funded check above now
        # that both live in the same transaction, kept as defense in depth).
        cur.execute(
            "UPDATE applications SET status = %s WHERE id = %s AND status <> 'funded'",
            (_DECISION_STATUS.get(outcome, outcome), app_id),
        )
        if outcome == "approve":
            # Security fix: accept_offer used to run fully anonymously for a
            # fresh accept -- fine for the legitimate no-account borrower
            # flow, except app_id is a sequential, guessable integer, so
            # anyone could accept/fund a STRANGER's approved application.
            # This one-time token is minted only now, held by the
            # borrower's own browser (decision response -> frontend state
            # -> accept call), and is the proof of ownership accept_offer
            # requires from a non-staff caller.
            accept_token = secrets.token_urlsafe(32)
            cur.execute(
                "UPDATE applications SET accept_token = %s WHERE id = %s",
                (accept_token, app_id),
            )

    if outcome == "approve":
        # W4: two-agent LangGraph (kg_reader -> assemble_disclosure), not a direct
        # call -- see disclosure_graph.py. Best-effort: a disclosure-service hiccup
        # must not fail the decision that already happened. Outside the
        # transaction on purpose -- an external call here must never hold
        # the coordination lock.
        try:
            disclosure_graph.auto_generate_offer(app_id)
        except Exception as e:  # noqa
            log.warning("auto offer-generation failed app_id=%s: %s", app_id, e)

    return DecisionOut(
        app_id=app_id,
        decision=outcome,
        score=int(round(resp.get("score") or 0)),  # DecisionOut.score is int
        adverse_action_reason=resp.get("reason"),
        accept_token=accept_token,
    )


def _already_decided_message(prior: dict) -> str:
    label = decision_state.format_outcome_label(prior["outcome"])
    reviewed_at = prior["reviewed_at"]
    when = reviewed_at.isoformat() if hasattr(reviewed_at, "isoformat") else str(reviewed_at)
    name = prior.get("reviewer_name") or prior.get("reviewer_role") or "a staff member"
    return (
        f"This application has already been {label} by {name} on {when}. "
        f"Reason: {prior['reason']}. The decision cannot be overwritten."
    )


@router.post("/{app_id}/review", response_model=DecisionOut)
def review_application(
    app_id: int,
    body: ReviewIn,
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    x_user_id: str | None = Header(default=None, alias="X-User-Id"),
):
    """Staff tool to resolve a "refer" decision (policies/underwriting_
    guidelines.md's manual-review band, score 600-659 or DTI 43-50%) into a
    real approve/deny. Staff-only, no borrower path at all -- this isn't a
    decision the applicant can make for themselves.

    Review fix: scoped to resolving an actual "refer" -- staff cannot use
    this to override a clean automated approve/deny (an application that
    never needed manual review in the first place).

    Review fix: once staff decides (approve or deny, with a reason), that
    decision is FINAL -- no staff member, not even a different one, may
    change it afterward, and no request that arrives after the first one
    ever writes anything, even if it races the first. manual_reviews.app_id
    is UNIQUE (db/migrations/0020); the INSERT below is an atomic
    check-and-write on it (same ON CONFLICT DO NOTHING + read-back pattern
    used throughout this codebase, e.g. payments.py's idempotency-key
    insert) -- only the request that actually wins the insert proceeds to
    change anything, and a loser is told exactly who decided, what, when,
    and why instead of a generic error.

    Audit fix: the "current outcome is still 'refer'" check is re-verified
    with a row lock (SELECT ... FOR UPDATE on decisions) inside the
    transaction below, not just the plain read further down -- that plain
    read is a fast pre-check only (avoids opening a transaction for the
    common already-decided case), not the authoritative guard. Without the
    re-check, a concurrent run_decision rerun changing decisions.outcome in
    the gap between the pre-check and this transaction would go undetected.
    """
    _require_staff(x_user_role, x_internal_token)

    rows = db.query("SELECT id, status FROM applications WHERE id = %s", (app_id,))
    if not rows:
        raise HTTPException(status_code=404, detail="application not found")

    existing = db.query("SELECT outcome FROM decisions WHERE app_id = %s", (app_id,))
    if not existing:
        raise HTTPException(status_code=422, detail="no decision exists yet for this application")
    current_outcome = existing[0]["outcome"]

    # Fast pre-check (cheap, the common case) -- the atomic INSERT inside the
    # transaction below is what actually enforces finality; this just avoids
    # opening a transaction for an application that's obviously already
    # decided, with the exact same message either way.
    prior = db.query(
        "SELECT outcome, reason, reviewer_name, reviewer_role, reviewed_at "
        "FROM manual_reviews WHERE app_id = %s",
        (app_id,),
    )
    if prior:
        raise HTTPException(status_code=409, detail=_already_decided_message(prior[0]))

    # Review fix: this endpoint resolves a refer -- it must not become a
    # backdoor to override an automated approve/deny that was never
    # eligible for manual review at all. Checked AFTER the already-decided
    # check above so a refer that staff already resolved (outcome is no
    # longer 'refer') still gets the more specific "already decided by X"
    # message instead of this more generic one.
    if current_outcome != "refer":
        raise HTTPException(
            status_code=422,
            detail=f"only a 'refer' decision can be reviewed by staff (current outcome: {current_outcome!r})",
        )

    # Best-effort: resolve the actual staff member's name for the audit
    # record and for a future "already decided by X" message -- falls back
    # to the role (still recorded either way) if this lookup can't resolve.
    reviewer_name = None
    if x_user_id:
        user_rows = db.query("SELECT display_name, username FROM users WHERE id = %s", (x_user_id,))
        if user_rows:
            reviewer_name = user_rows[0]["display_name"] or user_rows[0]["username"]

    accept_token = None
    with db.transaction() as cur:
        # Bug fix: an application that got funded WITHOUT ever going through
        # a manual review (e.g. an automated approve accepted directly) has
        # no manual_reviews row yet, so the pre-check above wouldn't catch
        # it -- staff recording a decision now would change decisions.outcome
        # on a loan that's already been boarded. SELECT ... FOR UPDATE takes
        # a row lock on applications for the rest of this transaction --
        # accept_offer's own `UPDATE applications ... WHERE status <>
        # 'funded'` targets the same row, so whichever of the two gets there
        # first now genuinely serializes the other instead of both reading a
        # stale "not funded" snapshot.
        cur.execute("SELECT status FROM applications WHERE id = %s FOR UPDATE", (app_id,))
        locked = cur.fetchall()
        if locked and locked[0]["status"] == "funded":
            raise HTTPException(
                status_code=422,
                detail="cannot decide on an already-funded application",
            )

        # Audit fix: the current_outcome == 'refer' check above reads via
        # db.query() on a separate, autocommitted connection BEFORE this
        # transaction even opens -- a concurrent run_decision could change
        # decisions.outcome in the gap between that read and here.
        # Architecture fix: origination-service is now the SOLE writer of
        # `decisions` (decision-service only proposes an outcome; see
        # graph.py::_node_persist), and run_decision's own transaction locks
        # this SAME applications row (FOR UPDATE) before it ever touches
        # decisions -- so the lock already held above is sufficient to
        # serialize the two endpoints; no separate lock on decisions itself
        # is needed. A plain re-read here (no lock of its own) is enough to
        # get the value as of this transaction's turn.
        cur.execute("SELECT outcome FROM decisions WHERE app_id = %s", (app_id,))
        locked_decision = cur.fetchall()
        locked_outcome = locked_decision[0]["outcome"] if locked_decision else None
        if locked_outcome != "refer":
            raise HTTPException(
                status_code=422,
                detail=f"only a 'refer' decision can be reviewed by staff (current outcome: {locked_outcome!r})",
            )

        # The real, atomic "first decision wins, forever" guard. ON CONFLICT
        # DO NOTHING means a losing concurrent request writes nothing at all
        # -- not a redundant row, not a second audit entry, nothing.
        cur.execute(
            "INSERT INTO manual_reviews (app_id, reviewer_role, reviewer_name, outcome, reason) "
            "VALUES (%s, %s, %s, %s, %s) ON CONFLICT (app_id) DO NOTHING "
            "RETURNING outcome, reason, reviewer_name, reviewer_role, reviewed_at",
            (app_id, x_user_role, reviewer_name, body.outcome, body.reason),
        )
        won = cur.fetchall()
        if not won:
            # Lost the race -- read back whichever request's decision
            # actually landed (inside this same transaction, so it's exactly
            # what committed) and report it the same way the pre-check does.
            cur.execute(
                "SELECT outcome, reason, reviewer_name, reviewer_role, reviewed_at "
                "FROM manual_reviews WHERE app_id = %s",
                (app_id,),
            )
            raise HTTPException(status_code=409, detail=_already_decided_message(cur.fetchall()[0]))

        cur.execute(
            "UPDATE decisions SET outcome = %s WHERE app_id = %s",
            (body.outcome, app_id),
        )
        # Same guard as run_decision's own status write: never regress an
        # already-funded application's status backward.
        cur.execute(
            "UPDATE applications SET status = %s WHERE id = %s AND status <> 'funded'",
            (_DECISION_STATUS.get(body.outcome, body.outcome), app_id),
        )
        if body.outcome == "approve":
            accept_token = secrets.token_urlsafe(32)
            cur.execute(
                "UPDATE applications SET accept_token = %s WHERE id = %s",
                (accept_token, app_id),
            )
        else:
            cur.execute(
                "UPDATE applications SET accept_token = NULL WHERE id = %s",
                (app_id,),
            )

    if body.outcome == "approve":
        # Same auto-offer as the automated approve path in run_decision above
        # -- a manually-approved application gets exactly the same
        # borrower-facing flow from here on. Best-effort and not part of the
        # transaction above: a disclosure-service hiccup must not undo an
        # already-committed manual review decision.
        try:
            disclosure_graph.auto_generate_offer(app_id)
        except Exception as e:  # noqa
            log.warning("auto offer-generation failed app_id=%s: %s", app_id, e)

    return DecisionOut(
        app_id=app_id,
        decision=body.outcome,
        adverse_action_reason=body.reason if body.outcome == "deny" else None,
        accept_token=accept_token,
    )


@router.get("/{app_id}/history")
def get_loan_history(
    app_id: int,
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
):
    """W4: the full borrower -> application -> decision -> offer graph for one
    application, via the kg.py traversal layer -- the concrete "trace this
    loan's whole history" capability the roadmap wanted. Staff only: this
    includes decision score/reason codes, the same underwriting-sensitive bar
    as get_application_financials above.
    """
    _require_staff(x_user_role, x_internal_token)
    history = kg.get_loan_history(app_id)
    if history is None:
        raise HTTPException(status_code=404, detail="application not found")
    return history


class AcceptIn(BaseModel):
    # Review fix: the one-time token minted onto the application when it was
    # approved (run_decision) -- stands in for a real session for the
    # legitimate no-account borrower flow. Optional so a staff-session accept
    # (re-accept of an already-funded application) needs no token.
    accept_token: str | None = None


@router.post("/{app_id}/accept")
def accept_offer(
    app_id: int,
    body: AcceptIn = AcceptIn(),
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
):
    # Security fix: this never checked that the application actually has an
    # approved decision on record, and never guarded against re-acceptance --
    # anyone who guessed an app_id could board/fund a real loan for an
    # application that was denied, still pending, or belongs to a stranger,
    # or re-board an already-funded one a second time.
    #
    # Review fix: each failure state below now gets its own specific,
    # honest message (workflow rules: SUBMITTED -> REVIEWED -> APPROVED ->
    # OFFER_CREATED -> OFFER_ACCEPTED -> BOARDED, or ... -> DENIED). These
    # checks read the same fields GET /applications/{id} already exposes
    # anonymously (decision, offer, status), so answering them here isn't a
    # new information disclosure -- only the actual fund-and-board action
    # below still requires real authorization.
    rows = db.query(
        "SELECT a.amount, a.term_months, a.status, a.accept_token, ap.name, "
        "o.apr, o.accepted_at, d.outcome "
        "FROM applications a LEFT JOIN applicants ap ON ap.id = a.applicant_id "
        "LEFT JOIN offers o ON o.app_id = a.id "
        "LEFT JOIN decisions d ON d.app_id = a.id "
        "WHERE a.id = %s ORDER BY o.id DESC",
        (app_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="application not found")
    r = rows[0]
    outcome = r.get("outcome")

    if r.get("status") == "funded":
        raise HTTPException(
            status_code=409,
            detail="This application has already been boarded.",
        )
    if outcome == "deny":
        reason = decision_state.get_deny_reason(app_id)
        raise HTTPException(
            status_code=422,
            detail=(
                "This application cannot be boarded because it was denied. "
                f"Reason: {reason or 'not on record'}."
            ),
        )
    if outcome != "approve":
        raise HTTPException(
            status_code=422,
            detail="This application must receive final approval before it can be boarded.",
        )
    if r.get("apr") is None:
        raise HTTPException(
            status_code=409,
            detail="Create an offer before boarding this application.",
        )
    # Note: an offer existing without accepted_at set is not a reachable
    # state in this system today -- accepting the offer and boarding the
    # loan happen as one atomic action below, so there is no separate
    # "offer created but not yet accepted" window to guard against; see
    # db/migrations/0021.

    rate = r["apr"]
    name = r.get("name") or "Borrower"

    # Security fix: a fresh accept used to run fully anonymously with no
    # ownership check at all -- app_id is a sequential, guessable integer, so
    # anyone could accept/fund a STRANGER's approved application. Staff or
    # the one-time accept_token (minted in run_decision, held only by the
    # borrower's own browser session) is now required.
    is_owner = bool(body.accept_token) and bool(r.get("accept_token")) and body.accept_token == r["accept_token"]
    if not _is_staff(x_user_role, x_internal_token) and not is_owner:
        raise HTTPException(status_code=403, detail="not authorized to accept this offer")

    # Security fix: two concurrent accepts on the same not-yet-funded
    # application both used to pass this same (stale-read) status check and
    # both board a loan. The UPDATE below is the real, atomic guard --
    # Postgres row-locks the application for the duration of the UPDATE, so
    # only ONE concurrent caller's WHERE status <> 'funded' can still be
    # true; the other gets zero rows back and never boards anything.
    # Boarding runs in the SAME transaction, so a mid-board failure leaves
    # status unfunded (safe to retry) instead of stuck funded-with-no-loan.
    # loans_app_id_key (db/migrations/0015) is the second, database-level
    # backstop for any other path that ever inserts a loan.
    with db.transaction() as cur:
        cur.execute(
            "UPDATE applications SET status = 'funded', accept_token = NULL "
            "WHERE id = %s AND status <> 'funded' RETURNING id",
            (app_id,),
        )
        if not cur.fetchall():
            raise HTTPException(
                status_code=409,
                detail="This application has already been boarded.",
            )
        cur.execute(
            "UPDATE offers SET accepted_at = now() WHERE app_id = %s AND accepted_at IS NULL",
            (app_id,),
        )
        try:
            loan_id = intake.board_to_servicing_tx(cur, app_id, name, r["amount"], rate, r["term_months"])
        except psycopg2.errors.UniqueViolation:
            # loans_app_id_key (db/migrations/0015) -- a loan already exists
            # for this application; surface that instead of a raw 500.
            raise HTTPException(status_code=409, detail="a loan already exists for this application")
    return {"loan_id": loan_id}
