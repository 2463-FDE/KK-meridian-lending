"""Offer / Truth-in-Lending disclosure generation (disclosure-service).

Write path (POST /offers) builds the offer + amortization schedule with float math and
persists an offers row via raw psycopg2 (matches the LOS write path). Read path
(GET /applications/{id}/offer) goes through SQLAlchemy.
"""
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import db, models, offer as offer_mod, schedule
from ..database import get_session
from ..schemas import Disclosure, OfferIn, OfferResponse, ScheduleRow

router = APIRouter(tags=["offers"])


@router.post("/offers", response_model=OfferResponse)
def create_offer(body: OfferIn):
    # W4 review fix: never trust a caller-supplied decision_id directly -- the FK
    # on offers.decision_id only proves that SOME decision with that id exists,
    # not that it belongs to this application_id. application_id=A + decision_id=B
    # with no real relation between them would have sailed through, leaking
    # applicant B's decision into application A's audit trail. decisions.app_id is
    # that table's own PK (one decision per application), so the only decision_id
    # that can ever legitimately apply here is the one derived from application_id
    # itself -- and it must actually be an approval, not just exist.
    decision_rows = db.query(
        "SELECT app_id FROM decisions WHERE app_id = %s AND outcome = 'approve'",
        (body.application_id,),
    )
    if not decision_rows:
        raise HTTPException(
            status_code=422,
            detail=f"no approved decision on record for application_id={body.application_id}",
        )
    decision_id = decision_rows[0]["app_id"]

    o = offer_mod.build_offer(body.principal, body.annual_rate, body.term_months)
    rows = schedule.amortization(body.principal, body.annual_rate, body.term_months)
    # W4: snapshot the fee rule version in effect right now, on this row, so a later
    # change to ORIGINATION_FEE_PCT can never retroactively change what this offer
    # is proven to have used.
    fee_pct_used = float(offer_mod.ORIGINATION_FEE_PCT)
    # persist via raw psycopg2 (matches origination's write path) — float money columns.
    # ON CONFLICT (decision_id): a retried/duplicated call (timeout retry, double
    # click) for the same decision updates the one canonical offer row instead of
    # minting a second one that ORDER BY id DESC would then silently prefer
    # (review finding -- see 0007_offers_decision_id_unique.sql).
    inserted = db.query(
        "INSERT INTO offers (app_id, decision_id, fee_pct_used, apr, finance_charge, "
        "monthly_payment, amount_financed, total_of_payments) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (decision_id) DO UPDATE SET "
        "  app_id = EXCLUDED.app_id, fee_pct_used = EXCLUDED.fee_pct_used, "
        "  apr = EXCLUDED.apr, finance_charge = EXCLUDED.finance_charge, "
        "  monthly_payment = EXCLUDED.monthly_payment, "
        "  amount_financed = EXCLUDED.amount_financed, "
        "  total_of_payments = EXCLUDED.total_of_payments "
        "RETURNING id",
        (body.application_id, decision_id, fee_pct_used, o["apr"], o["finance_charge"],
         o["monthly_payment"], o["amount_financed"], o["total_of_payments"]),
    )
    offer_id = inserted[0]["id"]
    disclosure = Disclosure(
        apr=o["apr"], finance_charge=o["finance_charge"],
        monthly_payment=o["monthly_payment"], amount_financed=o["amount_financed"],
        total_of_payments=o["total_of_payments"],
    )
    return OfferResponse(
        offer_id=offer_id, application_id=body.application_id,
        decision_id=decision_id, fee_pct_used=fee_pct_used,
        apr=o["apr"], finance_charge=o["finance_charge"],
        monthly_payment=o["monthly_payment"], total_of_payments=o["total_of_payments"],
        disclosure=disclosure, schedule=[ScheduleRow(**r) for r in rows],
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
