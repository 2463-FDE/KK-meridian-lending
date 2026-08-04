"""Offer / Truth-in-Lending disclosure generation (disclosure-service).

Write path (POST /offers) builds the offer + amortization schedule with float math and
persists an offers row via raw psycopg2 (matches the LOS write path). Read path
(GET /applications/{id}/offer) goes through SQLAlchemy.
"""
import psycopg2.errors
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import config, db, models, offer as offer_mod, schedule
from ..database import get_session
from ..schemas import Disclosure, OfferIn, OfferResponse, ScheduleRow

router = APIRouter(tags=["offers"])


@router.post("/offers", response_model=OfferResponse)
def create_offer(
    body: OfferIn,
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
):
    # Defense in depth: the network boundary (no host port -- see
    # docker-compose.yml) is the primary control; this is the fallback in case
    # that boundary is ever mistakenly reopened. An unset config token can
    # never match, so a deploy that forgets to set one fails closed.
    if not config.INTERNAL_SERVICE_TOKEN or x_internal_token != config.INTERNAL_SERVICE_TOKEN:
        raise HTTPException(status_code=401, detail="not authorized")

    # Security fix: principal/term_months/annual_rate used to come straight from
    # the caller with only an "is this application approved" check -- combined with
    # ON CONFLICT (decision_id) DO UPDATE, a repeat POST for an approved
    # application_id could overwrite the canonical offer with whatever numbers the
    # caller sent, and offer creation wasn't restricted to staff/services. Source
    # principal/term from the application's own record instead, same as the
    # auto-generation path (disclosure_graph.py) already does; annual_rate has no
    # per-applicant concept anywhere in this system, so it's never caller-supplied
    # either, just the same fixed default.
    app_rows = db.query(
        "SELECT amount, term_months FROM applications WHERE id = %s",
        (body.application_id,),
    )
    if not app_rows:
        raise HTTPException(
            status_code=404,
            detail=f"no application on record for application_id={body.application_id}",
        )
    principal = float(app_rows[0]["amount"])
    term_months = app_rows[0]["term_months"]
    annual_rate = 7.99

    o = offer_mod.build_offer(principal, annual_rate, term_months)
    rows = schedule.amortization(principal, annual_rate, term_months)
    # W4: snapshot the fee rule version in effect right now, on this row, so a later
    # change to ORIGINATION_FEE_PCT can never retroactively change what this offer
    # is proven to have used.
    fee_pct_used = float(offer_mod.ORIGINATION_FEE_PCT)

    # Review fix: the "is this application approved" check and the offer write
    # used to be two separate statements (a SELECT, then an INSERT) -- a
    # concurrent decision rerun could flip the outcome to 'deny' in the gap
    # between them, leaving an offer attached to a denied decision. Folding
    # the approval check into the INSERT's own SELECT ... FROM decisions
    # WHERE outcome = 'approve' makes the check and the write atomic: a row
    # is only ever inserted for a decision that is STILL approved at the
    # instant of the insert. decisions.app_id is that table's own PK (one
    # decision per application), so it doubles as the offer's decision_id --
    # never trust a caller-supplied decision_id directly (W4 review fix): the
    # FK alone only proves SOME decision with that id exists, not that it
    # belongs to this application_id.
    #
    # Review fix: ON CONFLICT ... DO UPDATE used to recompute APR/finance
    # charge/fee_pct_used from whatever the fee config happens to be right
    # now on every retried/duplicated call -- if the fee rule changed between
    # the original request and a retry, the borrower's canonical disclosure
    # would silently change underneath them. DO NOTHING instead, then fall
    # back to reading the already-stored row below -- a retry always gets
    # back the ORIGINAL terms, never a recomputed set.
    # Concurrency fix (borrower-workflow audit, found by a real-Postgres
    # test, not by inspection alone): offers.decision_id and offers.app_id
    # are TWO SEPARATE UNIQUE constraints (migrations 0009/0011), even
    # though this INSERT always sets them to the same value. ON CONFLICT
    # (decision_id) only suppresses a conflict on THAT constraint -- two
    # genuinely concurrent inserts for the same application can instead
    # collide on offers_app_id_key first, which this ON CONFLICT clause
    # does not target, raising an unhandled UniqueViolation (a raw 500)
    # instead of falling through to the read-back below. Caught explicitly
    # here and treated identically to the ON CONFLICT DO NOTHING case --
    # the constraint (whichever one fired) is still what guarantees
    # exactly one row; this just makes sure BOTH of its constraints are
    # handled gracefully, not just one.
    try:
        inserted = db.query(
            "INSERT INTO offers (app_id, decision_id, fee_pct_used, apr, finance_charge, "
            "monthly_payment, amount_financed, total_of_payments) "
            "SELECT d.app_id, d.app_id, %s, %s, %s, %s, %s, %s "
            "FROM decisions d WHERE d.app_id = %s AND d.outcome = 'approve' "
            "ON CONFLICT (decision_id) DO NOTHING "
            "RETURNING id, app_id, decision_id, fee_pct_used, apr, finance_charge, "
            "monthly_payment, amount_financed, total_of_payments",
            (fee_pct_used, o["apr"], o["finance_charge"], o["monthly_payment"],
             o["amount_financed"], o["total_of_payments"], body.application_id),
        )
    except psycopg2.errors.UniqueViolation:
        inserted = []
    created = bool(inserted)
    if inserted:
        row = inserted[0]
    else:
        # Either no approved decision exists for this application_id, or an
        # offer already exists for it (ON CONFLICT DO NOTHING -- see above).
        # decisions.app_id is this offer's decision_id, so it's also
        # body.application_id here.
        existing = db.query(
            "SELECT id, app_id, decision_id, fee_pct_used, apr, finance_charge, "
            "monthly_payment, amount_financed, total_of_payments "
            "FROM offers WHERE decision_id = %s",
            (body.application_id,),
        )
        if not existing:
            raise HTTPException(
                status_code=422,
                detail=f"no approved decision on record for application_id={body.application_id}",
            )
        row = existing[0]

    disclosure = Disclosure(
        apr=float(row["apr"]), finance_charge=float(row["finance_charge"]),
        monthly_payment=float(row["monthly_payment"]), amount_financed=float(row["amount_financed"]),
        total_of_payments=float(row["total_of_payments"]),
    )
    return OfferResponse(
        offer_id=row["id"], application_id=row["app_id"],
        decision_id=row["decision_id"], fee_pct_used=float(row["fee_pct_used"]),
        apr=float(row["apr"]), finance_charge=float(row["finance_charge"]),
        monthly_payment=float(row["monthly_payment"]), total_of_payments=float(row["total_of_payments"]),
        disclosure=disclosure, schedule=[ScheduleRow(**r) for r in rows],
        created=created,
    )


@router.get("/applications/{application_id}/offer", response_model=OfferResponse)
def get_offer(application_id: int, session: Session = Depends(get_session)):
    offer = session.scalar(
        select(models.Offer)
        .where(models.Offer.app_id == application_id)
        .order_by(models.Offer.id.desc())
    )
    if not offer:
        raise HTTPException(status_code=404, detail="no offer for this application")
    # Rebuild the display schedule from the persisted offer (Offer ORM only). Recover the
    # principal/term from the stored disclosure box and reuse the stored APR as the schedule
    # rate — the same shortcut the LOS read path takes. Float math throughout (D1).
    monthly_payment = offer.monthly_payment or 0.0
    total_of_payments = offer.total_of_payments or 0.0
    amount_financed = offer.amount_financed or 0.0
    # W4 review fix: use the fee rule actually snapshotted on THIS row, not
    # whatever ORIGINATION_FEE_PCT happens to be right now -- reading the live
    # constant here instead of the stored snapshot was exactly the drift this
    # column exists to prevent (a fee-schedule change would silently change the
    # recovered principal, and therefore the redisplayed schedule, for every
    # existing offer). Falls back to the live constant only for a legacy row
    # that predates the snapshot column.
    fee_pct = offer.fee_pct_used if offer.fee_pct_used is not None else float(offer_mod.ORIGINATION_FEE_PCT)
    principal = round(amount_financed / (1 - fee_pct), 2) if amount_financed else 0.0
    term_months = round(total_of_payments / monthly_payment) if monthly_payment else 0
    rows = schedule.amortization(principal, offer.apr or 7.99, term_months) if term_months else []
    disclosure = Disclosure(
        apr=offer.apr or 0, finance_charge=offer.finance_charge or 0,
        monthly_payment=monthly_payment, amount_financed=amount_financed,
        total_of_payments=total_of_payments,
    )
    return OfferResponse(
        offer_id=offer.id, application_id=application_id,
        decision_id=offer.decision_id, fee_pct_used=fee_pct,
        apr=offer.apr or 0, finance_charge=offer.finance_charge or 0,
        monthly_payment=monthly_payment, total_of_payments=total_of_payments,
        disclosure=disclosure, schedule=[ScheduleRow(**r) for r in rows],
    )
