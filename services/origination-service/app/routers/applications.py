"""Application intake, listing, detail, decisioning, and acceptance/boarding."""
import json

import httpx
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

#: Explicit, durable state for an application whose identity verification could
#: not be completed because kyc-service rejected our credentials. Terminal until
#: the applicant re-submits; run_decision refuses it. Plain TEXT column with no
#: CHECK constraint, so this needs no migration.
KYC_UNVERIFIED_STATUS = "kyc_unverified"

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
# router with NO auth check by default — a route here is anonymously reachable
# unless it explicitly gates itself (see _require_staff below). GET /{app_id}
# (ApplicationDetail) used to be exactly this kind of anonymous route despite
# returning applicant PII, decision outcome, offer terms, and manual-review
# rationale -- it is now staff-only (get_application, below). Before adding a
# new field to ApplicationListItem or any other response model in a route that
# is NOT staff-gated, ask:
#   1. Would this be sensitive if read by someone who only knows the app_id?
#      (income, SSN, DOB, credit score, decision reasoning, etc. -> yes)
#   2. If yes, put it on a route gated by _require_staff (see
#      get_application_financials below), not on the anonymous response.


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


# The five canonical TILA amounts. An offers row missing any of them is not a
# disclosure -- see _offer_disclosure_or_none and Gap F.
# Two different questions, deliberately not one list.
#
# TILA_MONETARY_FIELDS -- the historical four-box amounts. An accepted legacy
# offer that has these can still be DISPLAYED: those figures are what was
# disclosed, and hiding them because newer columns are absent would withhold a
# real disclosure over a bookkeeping gap.
TILA_MONETARY_FIELDS = (
    "apr", "finance_charge", "monthly_payment", "amount_financed", "total_of_payments",
)

# BOARDING_REQUIRED_FIELDS -- everything needed to board a contract that
# servicing can bill without inventing anything. The monetary amounts plus the
# contractual note rate and the persisted Model B schedule.
#
# Why the schedule fields belong here: under Model B the final payment differs
# from the regular one and cannot be recovered from any stored figure. Boarding
# an offer without it would leave servicing to regenerate the schedule with
# whatever generator is deployed -- which is the drift this PR exists to remove.
# NULL means "never recorded", so such an offer cannot board; an unaccepted one
# can be regenerated through the audited repair path instead.
BOARDING_REQUIRED_FIELDS = TILA_MONETARY_FIELDS + (
    "note_rate_pct", "regular_payment_count", "final_payment", "term_months",
    "schedule_version",
    # The principal the schedule was solved for. Boarding copies the stored
    # payments; opening the loan at a different principal would bill a schedule
    # that cannot amortize it to zero.
    "principal",
)

# Boarding readiness is what offer_ready reports and what accept enforces, so
# they cannot disagree: a caller told "ready" must not then hit a 409.
_CANONICAL_OFFER_FIELDS = BOARDING_REQUIRED_FIELDS


def _complete_offer_exists(app_id: int) -> bool:
    """Whether a usable TILA disclosure is on record for this application.

    PR #8 review: auto-generation is best-effort, so an approval could come
    back with an accept_token and no offer behind it. accept_offer already
    refuses to board without complete terms (Gap F), so this is not a funding
    hole -- but the caller had no way to know until it hit that 409. This is
    what DecisionOut.offer_ready reports.
    """
    checks = " AND ".join(f"{f} IS NOT NULL" for f in _CANONICAL_OFFER_FIELDS)
    rows = db.query(
        f"SELECT 1 FROM offers WHERE app_id = %s AND {checks} LIMIT 1", (app_id,)
    )
    return bool(rows)


def _offer_disclosure_or_none(offer, app_id: int) -> Disclosure | None:
    """Render an offers row as a Disclosure, or None if it is incomplete.

    Gap F (PR #6 review): this read used to be `offer.apr or 0` per field, so a
    row with a NULL amount was presented as a real disclosure quoting 0.00 --
    invented terms indistinguishable from genuine ones. Missing terms now
    render as "no offer", never as a number nobody calculated.

    Gated on TILA_MONETARY_FIELDS, not BOARDING_REQUIRED_FIELDS: this function
    answers "what was disclosed", and the four-box amounts are the whole of
    that answer. It briefly used the boarding list, which made a legacy offer
    render as no disclosure at all -- withholding figures that were genuinely
    disclosed because newer bookkeeping columns were absent. Whether those
    terms are complete enough to FUND is a different question, answered by
    _complete_offer_exists and reported separately as offer_ready; the UI
    disables Accept & board on that, so loosening this gate cannot let an
    unboardable offer through to a 409."""
    if offer is None:
        return None
    missing = [f for f in TILA_MONETARY_FIELDS if getattr(offer, f, None) is None]
    if missing:
        log.error(
            "incomplete offer row app_id=%s offer_id=%s missing=%s",
            app_id, getattr(offer, "id", None), ",".join(missing),
        )
        return None
    return Disclosure(
        note_rate_pct=getattr(offer, "note_rate_pct", None),
        apr=offer.apr, finance_charge=offer.finance_charge,
        monthly_payment=offer.monthly_payment, amount_financed=offer.amount_financed,
        total_of_payments=offer.total_of_payments,
        # Passed through as stored, including None. A legacy offer reports no
        # final payment rather than a recomputed one: shown beside four genuinely
        # disclosed amounts, a reconstructed figure is indistinguishable from a
        # disclosed one.
        regular_payment_count=getattr(offer, "regular_payment_count", None),
        final_payment=getattr(offer, "final_payment", None),
        term_months=getattr(offer, "term_months", None),
    )


@router.post("", response_model=ApplicationCreated)
def submit_application(body: ApplicationIn):
    payload = body.model_dump()
    # creates applicant+application rows; intake logs app_id/applicant_id only
    # (tests/test_intake_pii_not_logged.py). This comment used to claim full-PII
    # logging here — it was stale, see DEBT.md D5c.
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
        }, headers={"X-Internal-Token": config.INTERNAL_SERVICE_TOKEN})
        # Review round 6 (medium): these four used to be RECONSTRUCTED from the
        # single `cip_passed` flag -- `dob_verified = passed and not is_entity`
        # and so on. That is a guess about another service's audit record, and it
        # was wrong in both directions: an individual who passed on name and
        # address alone was reported `ssn_verified: true` while the persisted
        # `kyc_checks` row said false, and a partially-verified applicant was
        # reported as verifying nothing. Support and audit read this response.
        #
        # kyc-service now returns the four factors it actually recorded, so they
        # are passed through. The only remaining transformation is the fallback
        # below, which is all-false and says so.
        cip = {
            "name_verified": bool(resp.get("name_verified")),
            "dob_verified": bool(resp.get("dob_verified")),
            "address_verified": bool(resp.get("address_verified")),
            "ssn_verified": bool(resp.get("ssn_verified")),
        }
    except httpx.HTTPStatusError as e:
        # PR #18 review (high): a 401/403 from kyc-service is a CONFIGURATION
        # failure, not the transient hiccup the fallback below exists for, and
        # the two used to be indistinguishable. Every exception landed in one
        # `except`, logged a warning, and returned 200 "submitted" with all four
        # CIP booleans false -- so an unset or skewed INTERNAL_SERVICE_TOKEN
        # silently switched identity verification off for every applicant while
        # the flow looked entirely healthy and no kyc_checks row was written.
        #
        # An auth failure means we do not know who this applicant is and cannot
        # find out. Returning success would be a lie, so intake fails -- but the
        # applicant and application rows are already committed by
        # intake.create_application above, and a 503 on top of a persisted row
        # would leave an application that decisioning would happily pick up with
        # no KYC behind it.
        #
        # So the row is marked, explicitly and durably, before the error is
        # raised: `kyc_unverified` is a terminal-until-retried state that
        # run_decision refuses (see _require_persisted_kyc). The application is
        # visible for support, cannot advance, and re-submitting is safe.
        status = e.response.status_code if e.response is not None else None
        # 503 from kyc-service means it could not RECORD the result (review
        # finding: it used to swallow that and return 200 with check_id=-1, so an
        # applicant was told they were verified while no compliance row existed,
        # and the decision gate then blocked them later with no explanation).
        #
        # Grouped with the credential failures because the consequence is
        # identical and knowable: there is definitively no kyc_checks row, so the
        # application cannot advance. Marking it says that now, where the
        # applicant and support can see it, instead of at decision time.
        if status in (401, 403, 503):
            _mark_application_kyc_unverified(app_id, reason=f"kyc-service returned {status}")
            if status == 503:
                log.error(
                    "kyc-service could not record the CIP result app_id=%s -- intake "
                    "refused; the application has no compliance row and cannot proceed",
                    app_id,
                )
            else:
                log.error(
                    "kyc-service rejected our credentials (%s) app_id=%s -- intake refused; "
                    "check INTERNAL_SERVICE_TOKEN parity between origination and kyc-service",
                    status, app_id,
                )
            raise HTTPException(
                status_code=503,
                detail=("identity verification is unavailable: this application was "
                        "recorded but not verified, and cannot proceed. Please retry."),
            )
        # Any other HTTP status (5xx from kyc-service, say) is a genuine
        # service-side hiccup and keeps the original resilience: intake succeeds
        # with CIP all-false, because a KYC outage must not block an applicant
        # from submitting. run_decision still refuses to advance it until a KYC
        # result exists, so "submitted" never silently becomes "decided".
        log.warning("kyc-service returned %s for app_id=%s: %s", status, app_id, e)
    except Exception as e:  # noqa
        # Timeouts, connection errors, malformed responses -- transient by
        # assumption, same fallback as before.
        log.warning("kyc-service call failed: %s", e)
    return {"app_id": app_id, "status": "submitted", "kyc": KycOut(**cip), "access_token": access_token}


def _mark_application_kyc_unverified(app_id: int, reason: str) -> None:
    """Record that this application has no usable KYC result.

    Written as application status rather than a side table because every reader
    that could advance the application already consults `status`, so a new table
    would be one more thing each of them has to remember to check.
    """
    try:
        db.query(
            "UPDATE applications SET status = %s WHERE id = %s AND status = 'submitted'",
            (KYC_UNVERIFIED_STATUS, app_id),
        )
    except Exception as e:  # noqa
        # Best effort by necessity: if this write fails we still must not return
        # success, and the decision gate below is keyed on the ABSENCE of a
        # kyc_checks row rather than on this status, so it holds either way.
        log.error("could not mark app_id=%s as %s (%s): %s",
                  app_id, KYC_UNVERIFIED_STATUS, reason, e)


def _kyc_rows_for(app_id: int):
    """The CIP verdicts recorded for this application, best first."""
    return db.query(
        "SELECT cip_passed FROM kyc_checks WHERE application_id = %s "
        "ORDER BY cip_passed DESC NULLS LAST, id DESC LIMIT 1",
        (app_id,),
    )


def _attempt_kyc_recheck(app_id: int) -> None:
    """Run CIP once for an application that has no result, and swallow failure.

    Only for the MISSING case. A recorded failure is an answer, and re-running it
    would just ask the same question again with the same data.

    Failure here is deliberately silent: the caller re-reads the table and
    refuses on its own if this did not produce a row, so an exception would only
    replace a clear 409 about identity verification with a 500 about something
    the applicant cannot act on. What must not happen is this succeeding quietly
    while the row is still missing -- and it cannot, because nothing downstream
    trusts this function's return; the gate trusts the table.
    """
    rows = db.query(
        "SELECT a.applicant_id, p.name, p.dob, p.ssn, p.address, p.is_entity "
        "FROM applications a JOIN applicants p ON p.id = a.applicant_id "
        "WHERE a.id = %s",
        (app_id,),
    )
    if not rows:
        return
    r = rows[0]
    try:
        clients.post(clients.KYC_URL, "/kyc/check", {
            "application_id": app_id,
            "applicant_id": r["applicant_id"],
            "name": r["name"],
            # str() because these arrive as date/typed values off the row, and
            # kyc-service verifies presence, not format.
            "dob": str(r["dob"]) if r["dob"] else None,
            "ssn": r["ssn"],
            "address": r["address"],
            "entity_type": "llc" if r["is_entity"] else None,
        }, headers={"X-Internal-Token": config.INTERNAL_SERVICE_TOKEN})
        log.info("kyc recheck completed at decision time app_id=%s", app_id)
    except Exception as e:  # noqa
        # The type, never the message: a kyc-service error string can carry the
        # payload that produced it, and that payload is identity data.
        log.warning("kyc recheck failed at decision time app_id=%s (%s)",
                    app_id, type(e).__name__)


def _require_persisted_kyc(app_id: int) -> None:
    """Refuse to decide an application that has no persisted KYC result.

    PR #18 review: closing the intake hole is not enough on its own. An
    application that reached the database before KYC failed must not be able to
    walk into decisioning -- and decisioning is reachable directly at
    `POST /applications/{id}/decision`, not only through the intake response, so
    the gate has to live here rather than in the caller.

    What counts is a row in `kyc_checks` for THIS APPLICATION whose recorded
    verdict is a pass.

    Review round 6: this used to accept any row, on the reasoning that "a failed
    CIP is a real, recorded outcome the deny path is entitled to act on". No deny
    path acted on it. Nothing outside kyc-service read the result at all, so a
    recorded failure had exactly the same effect as a pass -- the application was
    decided, and could be approved, for an applicant this system had recorded as
    unidentified. A gate that admits a failure because something else will catch
    it is a gate only if that something else exists.

    Both cases it now refuses are the same case: we cannot show that this
    application's applicant was identified. Either no CIP result was persisted
    (KYC never ran, or could not be recorded), or one was and it did not pass, or
    it predates `db/migrations/0033` and does not record a verdict. A NULL is not
    a pass -- it is a row that does not say, which is not evidence of anything.

    Not a policy change dressed as a gate: a failed CIP should end as an adverse
    action with a reason, not a 409, and that path does not exist yet. Until it
    does, refusing to underwrite is the fail-closed direction, and the response
    says which of the two it is so support can tell them apart.

    Review finding: this used to key on the applicant, joining through
    `applications`, because `kyc_checks` had no `application_id`. That made the
    gate answer "has this APPLICANT ever been verified?" -- so a repeat applicant
    with an old check passed it even when this application's KYC call failed or
    never ran, and the logs recorded a block that had not happened. The evidence a
    regulator would ask for is the CIP result for this application, and the schema
    could not express it. `db/migrations/0032` adds the column; this now reads it.

    Historical rows may carry a NULL `application_id` where the link was never
    recorded and could not be inferred. Those do not satisfy this gate, and that
    is the correct direction: we cannot show CIP ran for that application, so we
    do not underwrite on it.
    """
    if not _kyc_rows_for(app_id):
        # Review round 7 (high): a KYC outage at intake time left a permanent
        # dead end. A timeout, a connection error or a non-503 5xx all take the
        # application -- deliberately, so an applicant is not turned away by our
        # outage -- but no kyc_checks row is written, and this gate then refuses
        # the application forever. The applicant sees "submitted", nothing
        # retries, and the only exits were a resubmission or a manual fix.
        #
        # The outage is over by now, most likely: intake and decision are
        # separate requests, usually minutes apart. So try once here before
        # refusing. Recovery belongs at the gate because the gate is what knows
        # the row is missing.
        _attempt_kyc_recheck(app_id)

    rows = db.query(
        "SELECT cip_passed FROM kyc_checks WHERE application_id = %s "
        # A passing row wins over a non-passing one for the same application:
        # ordering by cip_passed DESC NULLS LAST means a re-run that succeeded
        # settles it, rather than an earlier failure blocking forever.
        "ORDER BY cip_passed DESC NULLS LAST, id DESC LIMIT 1",
        (app_id,),
    )
    if rows and rows[0]["cip_passed"] is True:
        return

    if not rows:
        log.error("refusing to decide app_id=%s -- no persisted kyc_checks row", app_id)
        # "Re-submit" was the only advice that worked before the recheck above
        # existed. It no longer is, and it is the expensive one: a resubmission
        # is a second application, a second hard credit pull, and a support
        # ticket to reconcile the two. Retrying is now the cheap fix and usually
        # the working one, so it is what the message says.
        detail = ("identity verification has not completed for this application "
                  "yet, so it cannot be decided. Please try again shortly.")
    else:
        # Deliberately not logged with the factors that failed: which identity
        # element did not verify is exactly the kind of detail the logging rules
        # on this service keep out of log lines.
        log.error("refusing to decide app_id=%s -- recorded CIP did not pass", app_id)
        detail = ("identity verification did not pass for this application, so it "
                  "cannot be decided.")
    raise HTTPException(status_code=409, detail=detail)


@router.get("", response_model=Page[ApplicationListItem])
def list_applications(
    session: Session = Depends(get_session),
    status: str | None = Query(default=None),
    limit: int = Query(default=25, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
):
    # Staff only: this returns applicant PII and decision status for every
    # application. Gate before any query so there is no existence oracle.
    _require_staff(x_user_role, x_internal_token)
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
def get_application(
    app_id: int,
    session: Session = Depends(get_session),
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
):
    # Security fix (PR #6 review): this route used to be reachable anonymously
    # via the gateway's /los/* passthrough with no check at all -- app_id is
    # sequential/guessable, and the response includes applicant PII (name,
    # email, phone, address), loan amount/purpose, decision outcome, offer
    # terms, and (until this fix) staff manual-review rationale/reviewer
    # identity. Every real consumer of this route (frontend/app/underwriting/
    # [appId]/page.tsx, loan-assistant's /summary call) is already staff-only
    # -- the borrower-facing /apply flow never calls this route at all (it
    # gets its own status from POST /decision, /offer, /accept responses).
    # Staff check runs FIRST, before any DB lookup, so a non-staff caller gets
    # the same 403 whether or not app_id even exists -- no existence oracle.
    _require_staff(x_user_role, x_internal_token)
    a = session.get(models.Application, app_id)
    if not a:
        raise HTTPException(status_code=404, detail="application not found")
    applicant = a.applicant
    # Scoped to THIS application, not the applicant. Review finding: this read
    # took the latest row for the applicant, so staff opening a repeat
    # applicant's second application saw identity evidence from their first --
    # the exact mixing db/migrations/0032 exists to stop, still happening on the
    # screen a human actually looks at while the decision gate refused the same
    # application as unverified.
    kyc_row = session.scalar(
        select(models.KycCheck).where(models.KycCheck.application_id == app_id)
        .order_by(models.KycCheck.id.desc())
    )
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
        # Gap F (PR #6 review): these five used to be `offer.<field> or 0`, so
        # an incomplete offer row was rendered to the staff console as a real
        # disclosure showing 0.00 terms. An offer missing any canonical amount
        # is not a disclosure -- surface nothing rather than invented numbers.
        # (disclosure-service's own read path returns an explicit 409; this is
        # a staff detail view, so it degrades to "no offer yet" instead of
        # failing the whole application page.)
        offer=_offer_disclosure_or_none(offer, a.id),
        # Read from SQL rather than from the ORM row above on purpose: this must
        # agree with what accept_offer enforces, and accept re-reads the row
        # under a lock. Deriving it from `offer` here would be a second
        # implementation of the same rule -- the kind that drifts.
        offer_ready=_complete_offer_exists(a.id),
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
        f"SELECT a.id, a.applicant_id, a.amount, a.term_months, a.income, "
        f"{decision_state.ACCESS_TOKEN_FIELDS}, "
        "a.status, ap.name, ap.ssn "
        "FROM applications a LEFT JOIN applicants ap ON ap.id = a.applicant_id WHERE a.id = %s",
        (app_id,),
    )
    if not rows:
        raise HTTPException(status_code=404, detail="application not found")
    r = rows[0]

    existing = db.query("SELECT app_id FROM decisions WHERE app_id = %s", (app_id,))
    is_staff = _is_staff(x_user_role, x_internal_token)
    if existing:
        if not is_staff:
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
        # get reviewed and reversed indefinitely. (The funded/manual-final
        # checks that used to run here, unlocked, now live inside
        # decision_state.start_decision_attempt below -- locked, and run
        # immediately before decision-service is ever called instead of
        # only after it returns.)
        requested_by = x_user_role
    else:
        # Gap B: constant-time hash comparison against the stored sha256, plus
        # Postgres-clock expiry and single-use state -- never a plain `==` on a
        # plaintext column. Every failure mode (wrong / expired / already used /
        # never issued) collapses into the same generic 403 so an anonymous
        # caller learns nothing about the application from the response.
        is_owner = decision_state.verify_access_token(r, body.access_token)
        if not is_staff and not is_owner:
            raise HTTPException(status_code=403, detail="not authorized to request a decision for this application")
        requested_by = x_user_role if is_staff else "borrower"

    # PR #18 review: an application that reached the database before KYC failed
    # must not walk into decisioning. Checked here rather than in the intake
    # response because THIS route is directly reachable -- the gateway proxies
    # /los/* anonymously by design, so a caller with an app_id can ask for a
    # decision without ever seeing what intake returned.
    #
    # AFTER authorization, deliberately. It ran before it in the first version,
    # which reintroduced an oracle this route had already closed: an anonymous
    # caller guessing an app_id got 409 for a real application with no KYC row
    # and 403 otherwise, so the response distinguished "this application exists"
    # from "you may not ask" before the caller had proven anything. Every
    # unauthorized path on this route collapses to one generic 403 on purpose
    # (see the access-token comment above), and a check that runs earlier than
    # the trust boundary undoes that no matter how correct the check itself is.
    #
    # Still before start_decision_attempt and the decision-service call, so an
    # authorized-but-unverified application costs no bureau inquiry and leaves
    # no attempt row.
    _require_persisted_kyc(app_id)

    # PR #6 review (Finding 2): TXN A -- lock the application, recheck
    # funded/manual finality (the authoritative check -- everything above
    # is not), atomically recover a stale (crashed-process) attempt if one
    # exists, and create a fresh 'in_progress' attempt, all in one short
    # transaction, released BEFORE decision-service is ever called. A
    # request already blocked by finality performs no bureau/model work at
    # all. See decision_state.start_decision_attempt and
    # db/migrations/0023_decision_attempts.sql.
    attempt_id, bureau_request_key = decision_state.start_decision_attempt(app_id, requested_by)

    try:
        resp = clients.post(clients.DECISION_URL, "/decisions", {
            "application_id": app_id,
            "attempt_id": attempt_id,
            # Gap A: stable across an ambiguous-timeout retry, so the bureau
            # returns the original operation instead of pulling again.
            "bureau_request_key": bureau_request_key,
            "applicant_id": r.get("applicant_id"),
            "name": r.get("name"),
            "ssn": r.get("ssn") or "",
            "requested_amount": float(r.get("amount")),
            "term_months": r.get("term_months"),
            "annual_income": float(r.get("income") or 0),
            "monthly_debt": 0,            # not captured in the LOS today
            "credit_score": None,         # pulled downstream by decision-service
        }, headers={"X-Internal-Token": config.INTERNAL_SERVICE_TOKEN})
    except httpx.TimeoutException as e:
        # Ambiguous: decision-service may never have started, may still be
        # running, or may have finished with the response lost in transit --
        # this cannot be told apart. Release the attempt and let a retry create
        # a fresh one. Recording 'timeout' is what makes the retry reuse this
        # attempt's bureau_request_key (Gap A), so the retry recovers the
        # original pull instead of billing a second one.
        decision_state.mark_attempt_failed(attempt_id, "timeout")
        log.error("decision-service timed out app_id=%s attempt_id=%s: %s", app_id, attempt_id, e)
        raise HTTPException(status_code=502, detail="decision-service timed out -- please try again") from e
    except httpx.HTTPError as e:
        decision_state.mark_attempt_failed(attempt_id, "unavailable")
        log.error("decision-service unavailable app_id=%s attempt_id=%s: %s", app_id, attempt_id, e)
        raise HTTPException(status_code=502, detail="decision-service is unavailable -- please try again") from e

    # Security/correctness fix: decision-service must answer the SAME
    # attempt this request is currently waiting on -- a response that
    # doesn't match is never trusted enough to persist anything from.
    if resp.get("attempt_id") != attempt_id:
        decision_state.mark_attempt_failed(attempt_id, "invalid_response")
        log.error(
            "decision-service attempt_id mismatch app_id=%s expected=%s got=%s",
            app_id, attempt_id, resp.get("attempt_id"),
        )
        raise HTTPException(status_code=502, detail="decision-service returned an inconsistent response")

    outcome = resp["outcome"]

    # PR #6 review (Finding 2): TXN B -- lock again, recheck finality (the
    # one genuinely-concurrent race that can't be closed without holding a
    # lock across the network call above -- staff finalizing or funding
    # landing in the exact window while decision-service was computing),
    # confirm THIS attempt is still the live, active reservation
    # (state='in_progress' AND its lease has not passed -- see
    # decision_state.verify_attempt_still_active_locked), and only THEN
    # persist decisions + decision_events + mark the attempt completed, all
    # atomically. A late/duplicate computation
    # for an attempt that already expired-and-was-replaced (recovery ran
    # while this call was still in flight) is discarded here too -- it must
    # never overwrite whatever the replacement attempt already committed.
    # If finality now blocks this attempt, it is marked discarded and
    # NEITHER decisions NOR decision_events is written -- a discarded
    # attempt can never appear as a permanent decision event. Every discard
    # branch below exits the `with` block normally (not by raising inside
    # it) so its own discard-marking UPDATE actually commits; the
    # HTTPException is raised only after that commit succeeds.
    accept_token = None
    discard_error = None
    try:
        with db.transaction() as cur:
            # Global lock order: applications -> decision_attempts. Keep the
            # finality recheck first; reversing these two deadlocks TXN A.
            funded, manual = decision_state.recheck_finality_locked(cur, app_id)
            if not decision_state.verify_attempt_still_active_locked(cur, attempt_id):
                discard_error = (
                    409,
                    "this decision attempt is no longer active (expired or superseded) -- please retry",
                )

            if discard_error is not None:
                pass  # already set above -- attempt itself is no longer active
            elif funded:
                cur.execute(
                    "UPDATE decision_attempts SET state = 'discarded', completed_at = now(), "
                    "failure_code = 'funded', failure_detail = %s WHERE id = %s AND state = 'in_progress'",
                    (decision_state.sanitize_failure_detail("funded"), attempt_id),
                )
                discard_error = (422, "cannot rerun a decision on an already-funded application")
            elif manual:
                cur.execute(
                    "UPDATE decision_attempts SET state = 'discarded', completed_at = now(), "
                    "failure_code = 'superseded_by_staff', failure_detail = %s WHERE id = %s AND state = 'in_progress'",
                    (decision_state.sanitize_failure_detail("superseded_by_staff"), attempt_id),
                )
                discard_error = (409, decision_state.format_rerun_blocked_message(manual))
            else:
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
                # PR #6 review (Finding 2): decision-service no longer writes this
                # row itself (see decision-service/app/graph.py::_node_finalize) --
                # origination writes it here, in the SAME transaction as
                # `decisions`, only on the winning branch. attempt_id ties this
                # permanent audit row to the exact attempt that produced it
                # (db/migrations/0023_decision_attempts.sql).
                cur.execute(
                    "INSERT INTO decision_events "
                    "(app_id, requested_amount, term_months, annual_income, bureau_score, "
                    " model_score, model_version, top_features, decision, reason_codes, attempt_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    (
                        app_id,
                        r.get("amount"),
                        r.get("term_months"),
                        r.get("income"),
                        resp.get("bureau_score"),
                        resp.get("score"),
                        resp.get("model_version"),
                        json.dumps(resp.get("top_features")),
                        outcome,
                        json.dumps(resp.get("reason_codes") or []),
                        attempt_id,
                    ),
                )
                if outcome == "approve":
                    # Security fix: accept_offer used to run fully anonymously for a
                    # fresh accept -- fine for the legitimate no-account borrower
                    # flow, except app_id is a sequential, guessable integer, so
                    # anyone could accept/fund a STRANGER's approved application.
                    # This one-time token is minted only now, held by the
                    # borrower's own browser (decision response -> frontend state
                    # -> accept call), and is the proof of ownership accept_offer
                    # requires from a non-staff caller. See decision_state.
                    # issue_accept_token for hashing/expiry.
                    accept_token = decision_state.issue_accept_token(cur, app_id)
                else:
                    # Security fix (audit finding): a rerun landing on deny/refer
                    # used to leave a PREVIOUSLY minted token (from an earlier
                    # approve) still valid -- review_application already revoked it
                    # on its own non-approve branch, but this path had no
                    # equivalent, so the same application could be approved, then
                    # rerun to deny, while the old accept link still worked. Same
                    # helper both paths use now -- see decision_state.py.
                    decision_state.revoke_accept_token(cur, app_id)
                # Gap B: single-use. Consumed HERE, in the same transaction as
                # the decision it authorised -- so a rolled-back decision (or
                # the ambiguous-timeout retry path from Gap A, which never got
                # this far) leaves the token usable and the borrower is not
                # locked out of a decision they never received.
                decision_state.consume_access_token(cur, app_id)
                cur.execute(
                    "UPDATE decision_attempts SET state = 'completed', completed_at = now(), "
                    "bureau_reference_id = %s "
                    "WHERE id = %s AND state = 'in_progress'",
                    (resp.get("bureau_reference_id"), attempt_id),
                )
    except Exception as e:
        # PR #6 review, lease-invariant follow-up: a caught TXN-B
        # persistence failure (anything unexpected reaching here -- e.g. a
        # constraint violation) has already been rolled back by
        # db.transaction()'s own except/rollback by the time it propagates
        # out of the `with` block above. Mark the attempt failed in a
        # SEPARATE short transaction right away, rather than leaving it
        # 'in_progress' to be discovered only when its lease eventually
        # expires -- a retry can proceed immediately instead of waiting.
        # Lease expiry remains the fallback for the case this code can't
        # even reach (the process itself dying mid-transaction).
        # Type only: a database error's message carries the failing SQL, the
        # constraint name and the offending parameter VALUES, so logging the
        # exception itself would put decision inputs into the service log.
        log.error(
            "TXN B failed to persist app_id=%s attempt_id=%s error_type=%s",
            app_id, attempt_id, type(e).__name__,
        )
        try:
            decision_state.mark_attempt_failed(attempt_id, "persistence_error")
        except Exception as cleanup_exc:  # noqa
            # Cleanup is best-effort -- the lease is the fallback. Never let a
            # cleanup failure replace the generic 500 below, and log it
            # type-only for the same reason as above.
            log.error(
                "attempt cleanup failed app_id=%s attempt_id=%s error_type=%s",
                app_id, attempt_id, type(cleanup_exc).__name__,
            )
        raise HTTPException(status_code=500, detail="could not persist the decision -- please retry") from e

    if discard_error:
        raise HTTPException(status_code=discard_error[0], detail=discard_error[1])

    # None, not False: a deny/refer produces no offer by design, and reporting
    # "not ready" there would read like a failure rather than "not applicable".
    offer_ready = None
    if outcome == "approve":
        # W4: two-agent LangGraph (kg_reader -> assemble_disclosure), not a direct
        # call -- see disclosure_graph.py. Best-effort: a disclosure-service hiccup
        # must not fail the decision that already happened. Outside the
        # transaction on purpose -- an external call here must never hold
        # the coordination lock, and this only runs after TXN B has already
        # committed the permanent decision + audit event.
        try:
            disclosure_graph.auto_generate_offer(app_id)
        except Exception as e:  # noqa
            # ERROR, not WARNING: an approved application with no disclosure is
            # an operational event, not a hiccup -- the borrower cannot be
            # funded until someone generates one (POST /los/offer).
            log.error(
                "auto offer-generation failed app_id=%s error_type=%s", app_id, type(e).__name__
            )
        offer_ready = _complete_offer_exists(app_id)
        if not offer_ready:
            log.error(
                "approved application has no complete offer app_id=%s -- accept will be "
                "refused until one is generated", app_id,
            )

    return DecisionOut(
        app_id=app_id,
        decision=outcome,
        score=int(round(resp.get("score") or 0)),  # DecisionOut.score is int
        adverse_action_reason=resp.get("reason"),
        accept_token=accept_token,
        offer_ready=offer_ready,
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
            accept_token = decision_state.issue_accept_token(cur, app_id)
        else:
            decision_state.revoke_accept_token(cur, app_id)

    # None, not False: a deny/refer produces no offer by design, and reporting
    # "not ready" there would read like a failure rather than "not applicable".
    offer_ready = None
    if body.outcome == "approve":
        # Same auto-offer as the automated approve path in run_decision above
        # -- a manually-approved application gets exactly the same
        # borrower-facing flow from here on. Best-effort and not part of the
        # transaction above: a disclosure-service hiccup must not undo an
        # already-committed manual review decision.
        try:
            disclosure_graph.auto_generate_offer(app_id)
        except Exception as e:  # noqa
            log.error(
                "auto offer-generation failed app_id=%s error_type=%s", app_id, type(e).__name__
            )
        offer_ready = _complete_offer_exists(app_id)
        if not offer_ready:
            log.error(
                "staff-approved application has no complete offer app_id=%s -- accept will "
                "be refused until one is generated", app_id,
            )

    return DecisionOut(
        app_id=app_id,
        decision=body.outcome,
        adverse_action_reason=body.reason if body.outcome == "deny" else None,
        accept_token=accept_token,
        offer_ready=offer_ready,
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


# Shared by the pre-check read below and the locked re-check inside the
# transaction -- token_live is evaluated by Postgres's own now(), never
# Python's, so app-host clock skew can never make a token look valid/
# invalid to the wrong side of the check (see decision_state.py).
_ACCEPT_TOKEN_FIELDS = (
    "accept_token_hash, accept_token_consumed_at, "
    "(accept_token_expires_at IS NOT NULL AND accept_token_expires_at > now()) AS token_live"
)


@router.post("/{app_id}/accept")
def accept_offer(
    app_id: int,
    x_user_role: str | None = Header(default=None, alias="X-User-Role"),
    x_internal_token: str | None = Header(default=None, alias="X-Internal-Token"),
    # Security fix (follow-up audit): the one-time token minted onto the
    # application when it was approved (run_decision/review_application)
    # used to travel as a JSON body field -- not itself leaked into any
    # access log (bodies aren't logged), but the SAME credential also
    # traveled as a URL query parameter on the sibling GET .../offer route,
    # which was proven to leak. Both routes now use the identical transport
    # -- a header, never a query string, never part of a URL -- so this
    # credential can't drift into an unsafe transport again on one route
    # while "fixed" on the other. Optional so a staff-session accept (no
    # token) still works. Only ever compared against its stored sha256
    # hash -- see decision_state.verify_accept_token. The raw value is
    # never persisted anywhere; it exists only in the borrower's browser
    # and this one request.
    x_offer_accept_token: str | None = Header(default=None, alias="X-Offer-Accept-Token"),
):
    # Security fix: this never checked that the application actually has an
    # approved decision on record, and never guarded against re-acceptance --
    # anyone who guessed an app_id could board/fund a real loan for an
    # application that was denied, still pending, or belongs to a stranger,
    # or re-board an already-funded one a second time.
    #
    # Review fix: each failure state below gets its own specific, honest
    # message (workflow rules: SUBMITTED -> REVIEWED -> APPROVED ->
    # OFFER_CREATED -> OFFER_ACCEPTED -> BOARDED, or ... -> DENIED) -- but
    # only for a caller who already proved ownership (or is staff) via the
    # gate immediately above. GET /applications/{id} is staff-only now too
    # (see get_application), so this is no longer "the same fields anonymous
    # elsewhere" -- these specific messages are themselves gated.
    #
    # This first read is a FAST PRE-CHECK only (cheap, the common case, and
    # avoids opening a transaction for an obviously-bad request) -- it is
    # NOT the authoritative check. Everything it reads is re-verified fresh
    # under a real row lock inside the transaction below, because all of it
    # (status, decision outcome, token validity) can change in the gap
    # between this read and that lock.
    rows = db.query(
        f"SELECT a.amount, a.term_months, a.status, {_ACCEPT_TOKEN_FIELDS}, ap.name, "
        "o.id AS offer_id, o.note_rate_pct, o.apr, o.finance_charge, o.monthly_payment, "
        "o.regular_payment_count, o.final_payment, o.term_months, o.schedule_version, "
        # `o.principal` aliased: `a.amount` is the requested amount and this is
        # the principal the stored schedule was solved for. Both are read here,
        # and confusing them is exactly the defect this alias prevents.
        "o.principal AS offer_principal, "
        "o.amount_financed, o.total_of_payments, o.accepted_at, d.outcome "
        "FROM applications a LEFT JOIN applicants ap ON ap.id = a.applicant_id "
        "LEFT JOIN offers o ON o.app_id = a.id "
        "LEFT JOIN decisions d ON d.app_id = a.id "
        "WHERE a.id = %s ORDER BY o.id DESC",
        (app_id,),
    )
    # Security fix (PR #6 review, follow-up): the state-revealing branches
    # below (funded / denied+reason / not-approved / no-offer) used to run
    # before ANY ownership check -- a stranger with just a guessed app_id and
    # no token at all could learn an application's full workflow state,
    # including another applicant's specific denial reason. Ownership
    # (hash-match only, existence check folded in) is now proven FIRST, for
    # a non-staff caller, before anything about this app_id is revealed --
    # same 403 whether the app doesn't exist, was never approved (so never
    # had a token), or belongs to someone else. This is intentionally just a
    # hash-match (not the full expiry/consumed verify_accept_token below) --
    # a caller who once legitimately held this application's token is not a
    # stranger, even if that token has since expired or been consumed.
    if not _is_staff(x_user_role, x_internal_token):
        if not rows or not decision_state.accept_token_hash_matches(
            rows[0].get("accept_token_hash"), x_offer_accept_token
        ):
            raise HTTPException(status_code=403, detail="not authorized to accept this offer")
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
    if r.get("offer_id") is None:
        raise HTTPException(
            status_code=409,
            detail="Create an offer before boarding this application.",
        )
    # Gap F: an offer row that EXISTS but is missing canonical terms is a
    # different problem from having no offer at all, and deserves a different
    # answer -- the borrower needs a regenerated offer, not a first one. The
    # authoritative check is the locked re-read below; this fast path just
    # avoids opening a transaction for a row already known to be unusable.
    # `principal` arrives under an alias in this projection (see the SELECT
    # above), so the readiness check has to look for it by that name or it would
    # report every offer as missing a column the row actually has.
    incomplete_precheck = [
        f for f in _CANONICAL_OFFER_FIELDS
        if r.get("offer_principal" if f == "principal" else f) is None
    ]
    if incomplete_precheck:
        raise HTTPException(
            status_code=409,
            detail=(
                "This offer is incomplete and cannot be boarded. Missing required "
                f"disclosure terms: {', '.join(incomplete_precheck)}. Generate a new offer."
            ),
        )

    name = r.get("name") or "Borrower"

    # Security fix: a fresh accept used to run fully anonymously with no
    # ownership check at all -- app_id is a sequential, guessable integer, so
    # anyone could accept/fund a STRANGER's approved application. Staff or
    # the one-time accept_token (minted in run_decision/review_application,
    # held only by the borrower's own browser session) is now required.
    # Fast-path rejection only -- see the authoritative re-check below.
    if not _is_staff(x_user_role, x_internal_token):
        ok, status_code, message = decision_state.verify_accept_token(r, x_offer_accept_token)
        if not ok:
            raise HTTPException(status_code=status_code, detail=message)

    # Security fix (audit finding): two concurrent accepts on the same
    # not-yet-funded application, or two concurrent accepts racing to use
    # the SAME token, both used to be able to board a loan -- the old code
    # only re-verified `status <> 'funded'` atomically; the token, decision
    # outcome, and offer state were all read once, before the transaction,
    # and trusted stale. Everything that must still be true at the instant
    # of boarding is now re-verified here under a real row lock:
    #   - applications.status is still not 'funded' (FOR UPDATE)
    #   - decisions.outcome is still 'approve' (a rerun/correction could
    #     have flipped it in the gap above -- and would have revoked the
    #     token too, but re-checking the outcome directly is the real
    #     invariant, not just a side effect of the token being gone)
    #   - the token (if this isn't a staff call) still hashes to the same
    #     value, is not expired (Postgres's own now()), and is not already
    #     consumed
    #   - the offer is still on record with a rate
    # loans_app_id_key (db/migrations/0015) remains a second, database-level
    # backstop for any other path that ever inserts a loan.
    with db.transaction() as cur:
        cur.execute(
            f"SELECT status, {_ACCEPT_TOKEN_FIELDS} FROM applications WHERE id = %s FOR UPDATE",
            (app_id,),
        )
        locked_rows = cur.fetchall()
        if not locked_rows:
            raise HTTPException(status_code=404, detail="application not found")
        locked = locked_rows[0]
        if locked["status"] == "funded":
            raise HTTPException(
                status_code=409,
                detail="This application has already been boarded.",
            )

        cur.execute("SELECT outcome FROM decisions WHERE app_id = %s", (app_id,))
        dec_rows = cur.fetchall()
        locked_outcome = dec_rows[0]["outcome"] if dec_rows else None
        if locked_outcome != "approve":
            raise HTTPException(
                status_code=422,
                detail="This application is no longer approved and cannot be boarded.",
            )

        if not _is_staff(x_user_role, x_internal_token):
            ok, status_code, message = decision_state.verify_accept_token(locked, x_offer_accept_token)
            if not ok:
                raise HTTPException(status_code=status_code, detail=message)

        # Re-read the offer fresh under the lock too -- there is no
        # offer-edit/cancel endpoint in this system today, so its rate
        # cannot actually change underneath us, but the accepted_at
        # condition matters: a second racing request must not board against
        # an offer this same transaction is about to mark accepted.
        cur.execute(
            "SELECT note_rate_pct, regular_payment_count, final_payment, term_months, "
            "schedule_version, apr, finance_charge, monthly_payment, amount_financed, "
            "total_of_payments, principal "
            "FROM offers WHERE app_id = %s AND accepted_at IS NULL ORDER BY id DESC LIMIT 1",
            (app_id,),
        )
        offer_rows = cur.fetchall()
        if not offer_rows:
            raise HTTPException(
                status_code=409,
                detail="Create an offer before boarding this application.",
            )
        # Gap F (PR #6 review): this used to check `apr IS NULL` alone, so an
        # offer row missing finance_charge/monthly_payment/amount_financed/
        # total_of_payments still boarded a real loan -- funding terms the
        # borrower was never shown a complete disclosure for. All five
        # canonical amounts must be present, checked here under the row lock.
        incomplete = [f for f in _CANONICAL_OFFER_FIELDS if offer_rows[0][f] is None]
        if incomplete:
            log.error(
                "refusing to board on an incomplete offer app_id=%s missing=%s",
                app_id, ",".join(incomplete),
            )
            raise HTTPException(
                status_code=409,
                detail=(
                    "This offer is incomplete and cannot be boarded. Missing required "
                    f"disclosure terms: {', '.join(incomplete)}. Generate a new offer."
                ),
            )
        # Review fix (PR #10): this used to board `offers.apr`. Once apr became
        # the true actuarial rate that meant servicing amortized the loan at the
        # DISCLOSED rate instead of the contractual one, billing 452.94 a month
        # against a disclosure that said 439.35 -- 652 more over a 48-month term,
        # against the borrower. It was wrong before that change too (it boarded
        # the old add-on ratio and billed under), so this is not a regression the
        # APR fix introduced so much as one it made harmful.
        #
        # Board the note rate: the contractual rate the payment schedule was
        # actually calculated on (db/migrations/0030). servicing amortizes
        # whatever it is given, so this is the number that decides what the
        # borrower is billed.
        rate = offer_rows[0]["note_rate_pct"]
        if rate is None:
            # Refuse rather than fall back to apr. A pre-0030 row that escaped
            # the back-fill has no recorded contractual rate, and guessing one
            # is how the borrower ends up on terms nobody disclosed.
            log.error("refusing to board an offer with no note_rate_pct app_id=%s", app_id)
            raise HTTPException(
                status_code=409,
                detail=(
                    "This offer does not record the contractual rate it was written at "
                    "and cannot be boarded. Generate a new offer."
                ),
            )

        cur.execute(
            "UPDATE applications SET status = 'funded', accept_token_hash = NULL, "
            "accept_token_expires_at = NULL, accept_token_consumed_at = now() "
            "WHERE id = %s AND status <> 'funded'",
            (app_id,),
        )
        cur.execute(
            "UPDATE offers SET accepted_at = now() WHERE app_id = %s AND accepted_at IS NULL",
            (app_id,),
        )
        offer = offer_rows[0]
        try:
            loan_id = intake.board_to_servicing_tx(
                cur, app_id, name,
                # The OFFER's principal, for the same reason as its term below:
                # it is the amount the stored schedule was actually solved for.
                # This used to board `applications.amount`. The two agree today
                # -- the offer is built from the application's own record -- but
                # if the requested amount were corrected after the offer was
                # written, or a counteroffer ever carried a different principal,
                # the loan would open at one principal while billing a schedule
                # calculated for another, and the balance would never amortize
                # to zero. Review finding on PR #10.
                #
                # Legacy rows have no stored principal; they cannot board at all
                # (BOARDING_REQUIRED_FIELDS), so the fallback here is only ever
                # reached if that gate is loosened, and it keeps today's
                # behaviour rather than a NULL.
                offer["principal"] if offer["principal"] is not None else r["amount"],
                rate,
                # The OFFER's contractual term, not the application's requested
                # one. They agree today -- the offer is built from the
                # application's term and the stored value is server-derived --
                # but only one of them is the term the schedule below was
                # actually solved for, and loans_schedule_term_agrees checks the
                # count against whatever is boarded here. Reading the requested
                # term would make a future counteroffer board a schedule filed
                # under the wrong term.
                offer["term_months"],
                # Copied, never recomputed. Under Model B the final payment
                # absorbs the cent residue and cannot be recovered from any
                # other stored figure, so a servicing-side recomputation would
                # not merely risk drift -- it could not reproduce these amounts
                # at all.
                regular_payment=offer["monthly_payment"],
                regular_payment_count=offer["regular_payment_count"],
                final_payment=offer["final_payment"],
                schedule_version=offer["schedule_version"],
            )
        except psycopg2.errors.UniqueViolation:
            # loans_app_id_key (db/migrations/0015) -- a loan already exists
            # for this application; surface that instead of a raw 500.
            raise HTTPException(status_code=409, detail="a loan already exists for this application")
    return {"loan_id": loan_id}
