"""Application intake, listing, detail, decisioning, and acceptance/boarding."""
from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .. import clients, db, fair_lending, intake, models
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
)

log = get_logger("applications")
router = APIRouter(prefix="/applications", tags=["applications"])

# Roles allowed to see underwriting-sensitive fields (income, employment_years).
# Mirrors the staff role set the gateway already enforces for /assistant/*.
_STAFF_ROLES = {"csr", "underwriter", "admin"}

# NOTE: the gateway's /los/{path:path} route (gateway/app/main.py) proxies to this
# router with NO auth check — an applicant can check their own status without an
# account, so anyone who guesses an app_id can hit any GET route here anonymously.
# Before adding a new field to ApplicationDetail, ApplicationListItem, or any other
# response model returned by a route in this file, ask:
#   1. Would this be sensitive if read by someone who only knows the app_id?
#      (income, SSN, DOB, credit score, decision reasoning, etc. -> yes)
#   2. If yes, put it on a separate endpoint gated by _STAFF_ROLES (see
#      get_application_financials below), not on the public response.


@router.post("", response_model=ApplicationCreated)
def submit_application(body: ApplicationIn):
    payload = body.model_dump()
    app_id = intake.create_application(payload)  # creates applicant+application rows, logs full PII (D5 — KEEP)
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
    return {"app_id": app_id, "status": "submitted", "kyc": KycOut(**cip)}


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
):
    """W8: ZIP-level disparate-impact screen (fair_lending.py). Registered
    before /{app_id} -- a literal path segment must be matched ahead of a
    catch-all path parameter, or "fair-lending" would be parsed as an app_id
    and 422 on the int conversion instead of ever reaching this route.
    Staff only: approval-rate breakdowns are underwriting-sensitive, same bar
    as get_application_financials below.
    """
    if x_user_role not in _STAFF_ROLES:
        raise HTTPException(status_code=403, detail="staff only")
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
):
    # Staff only: income/employment_years are underwriting inputs, not borrower
    # status data. The gateway forwards X-User-Role for authenticated sessions
    # (see gateway/app/main.py _proxy); anonymous /los/* callers send none.
    if x_user_role not in _STAFF_ROLES:
        raise HTTPException(status_code=403, detail="staff only")
    a = session.get(models.Application, app_id)
    if not a:
        raise HTTPException(status_code=404, detail="application not found")
    return ApplicationFinancials(income=a.income, employment_years=a.employment_years)


@router.post("/{app_id}/decision", response_model=DecisionOut)
def run_decision(app_id: int):
    rows = db.query(
        "SELECT a.id, a.applicant_id, a.amount, a.term_months, a.income, ap.name, ap.ssn "
        "FROM applications a LEFT JOIN applicants ap ON ap.id = a.applicant_id WHERE a.id = %s",
        (app_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="application not found")
    r = rows[0]
    # Decisioning moved to decision-service; it persists the (outcome-only) decisions row.
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
    })
    outcome = resp["outcome"]
    if outcome == "approve":
        _auto_generate_offer(app_id, r)
    return DecisionOut(
        app_id=app_id,
        decision=outcome,
        score=int(round(resp.get("score") or 0)),  # DecisionOut.score is int
        adverse_action_reason=resp.get("reason"),
    )


def _auto_generate_offer(app_id: int, r: dict) -> None:
    """W4: auto-build the offer + TILA disclosure right after approval, instead of
    leaving it as a separate manual step. Best-effort -- a disclosure-service hiccup
    must not fail the decision that already happened (same resilience pattern as the
    KYC call in submit_application above); the loan officer can still build it
    manually via POST /los/offer if this fails.
    """
    try:
        clients.post(clients.DISCLOSURE_URL, "/offers", {
            "application_id": app_id,
            "decision_id": app_id,  # decisions.app_id is that table's PK
            "principal": float(r.get("amount")),
            "term_months": r.get("term_months"),
            "annual_rate": 7.99,  # no per-applicant rate exists elsewhere -- same default make_offer() uses
        })
    except Exception as e:  # noqa
        log.warning("auto offer-generation failed app_id=%s: %s", app_id, e)


@router.post("/{app_id}/accept")
def accept_offer(app_id: int):
    rows = db.query(
        "SELECT a.amount, a.term_months, ap.name, o.apr "
        "FROM applications a LEFT JOIN applicants ap ON ap.id = a.applicant_id "
        "LEFT JOIN offers o ON o.app_id = a.id WHERE a.id = %s ORDER BY o.id DESC",
        (app_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="application not found")
    r = rows[0]
    rate = r.get("apr") or 7.99
    loan_id = intake.board_to_servicing(
        app_id, r.get("name") or "Borrower", r["amount"], rate, r["term_months"]
    )
    db.query("UPDATE applications SET status = 'funded' WHERE id = %s", (app_id,))
    return {"loan_id": loan_id}
