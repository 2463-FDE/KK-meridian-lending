"""Offer / Truth-in-Lending disclosure generation (disclosure-service).

Write path (POST /offers) builds the offer + amortization schedule with float math and
persists an offers row via raw psycopg2 (matches the LOS write path). Read path
(GET /applications/{id}/offer) goes through SQLAlchemy.
"""
import psycopg2.errors
from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import apr, config, db, fees, models, offer as offer_mod, schedule
from ..database import get_session
from ..logging_config import get_logger
from ..schemas import Disclosure, OfferIn, OfferResponse, ScheduleRow

log = get_logger("offers")
router = APIRouter(tags=["offers"])

# The five amounts that make a row a TILA disclosure. A row missing any of them
# is not an offer (db/init/001_schema.sql: offers_canonical_terms_present).
CANONICAL_TERMS = (
    "apr", "finance_charge", "monthly_payment", "amount_financed", "total_of_payments",
)

_OFFER_COLUMNS = (
    "id", "app_id", "decision_id", "fee_pct_used", "note_rate_pct", "apr", "finance_charge",
    "monthly_payment", "amount_financed", "total_of_payments", "accepted_at",
)
_OFFER_FIELDS = ", ".join(_OFFER_COLUMNS)
# Qualified form, for the repair statement's UPDATE ... FROM decisions: `app_id`
# is a column of BOTH tables there, so an unqualified list is ambiguous.
_OFFER_FIELDS_Q = ", ".join(f"o.{c}" for c in _OFFER_COLUMNS)


def missing_terms(row) -> list[str]:
    """Which canonical terms this offer row is missing, in disclosure order."""
    return [name for name in CANONICAL_TERMS if row[name] is None]


def _repair_incomplete_offer(row, missing, terms, fee_pct_used, application_id):
    """Regenerate the canonical terms of an existing INCOMPLETE offer, in place.

    Only reachable for a row that is already missing at least one canonical
    term -- migration 0026 documents these as pre-existing damage that predates
    the CHECK constraint. It is the ONLY write path that touches an offer after
    creation, and it is deliberately narrow:

      * an ACCEPTED offer is immutable -- refused, never rewritten, because the
        borrower has already been bound to whatever it says;
      * the WHERE clause re-asserts BOTH `accepted_at IS NULL` and "at least
        one term is still NULL", so this can never rewrite a complete offer and
        never races an accept that lands between the read and the write;
      * the decision must STILL be an approval at the instant of the write, the
        same standard the create path holds itself to;
      * UPDATE and audit row are one data-modifying-CTE statement, so an
        unaudited repair cannot exist (the connection is autocommit).

    The repaired row carries TODAY's fee rule, not the original one: an
    incomplete row has no honest terms to preserve, so this is a new disclosure,
    recorded as such in audit_logs.
    """
    if row["accepted_at"] is not None:
        log.error(
            "refusing to repair an ACCEPTED incomplete offer offer_id=%s application_id=%s missing=%s",
            row["id"], application_id, ",".join(missing),
        )
        raise HTTPException(
            status_code=409,
            detail=(
                "This offer is incomplete but has already been accepted, so its terms "
                "cannot be regenerated. Escalate for manual remediation."
            ),
        )

    repaired = db.query(
        "WITH repaired AS ("
        "  UPDATE offers o"
        "     SET fee_pct_used = %s, note_rate_pct = %s, apr = %s, finance_charge = %s,"
        "         monthly_payment = %s, amount_financed = %s, total_of_payments = %s,"
        # A repair writes the COMPLETE Model B schedule, not just the four-box
        # amounts. Repairing the monetary fields while leaving the schedule NULL
        # would produce a row that displays fine and still cannot board -- the
        # exact half-fixed state BOARDING_REQUIRED_FIELDS exists to prevent.
        "         regular_payment_count = %s, final_payment = %s, term_months = %s,"
        "         schedule_version = %s"
        "    FROM decisions d"
        "   WHERE o.decision_id = d.app_id AND d.app_id = %s AND d.outcome = 'approve'"
        "     AND o.accepted_at IS NULL"
        "     AND (o.apr IS NULL OR o.finance_charge IS NULL OR o.monthly_payment IS NULL"
        "          OR o.amount_financed IS NULL OR o.total_of_payments IS NULL"
        "          OR o.note_rate_pct IS NULL OR o.final_payment IS NULL"
        "          OR o.regular_payment_count IS NULL OR o.term_months IS NULL)"
        f"  RETURNING {_OFFER_FIELDS_Q}"
        "), audited AS ("
        "  INSERT INTO audit_logs (actor, action, detail)"
        "  SELECT 'disclosure-service', 'offer.incomplete_terms_repaired',"
        "         'offer_id=' || r.id || ' app_id=' || r.app_id || ' decision_id=' || r.decision_id"
        "         || ' missing=' || %s || ' fee_pct_used=' || r.fee_pct_used"
        "    FROM repaired r"
        ")"
        "SELECT * FROM repaired",
        (fee_pct_used, float(fees.NOTE_RATE_PCT), terms["apr"], terms["finance_charge"],
         terms["monthly_payment"], terms["amount_financed"], terms["total_of_payments"],
         terms["regular_payment_count"], terms["final_payment"],
         terms["regular_payment_count"] + 1, fees.SCHEDULE_VERSION,
         application_id, ",".join(missing)),
    )
    if repaired:
        log.warning(
            "repaired incomplete offer offer_id=%s application_id=%s missing=%s",
            repaired[0]["id"], application_id, ",".join(missing),
        )
        return repaired[0]

    # Nothing updated. Either a concurrent caller repaired it first, or it was
    # accepted in between, or the decision is no longer an approval. Re-read
    # and answer from what is actually on the row now.
    now = db.query(
        f"SELECT {_OFFER_FIELDS} FROM offers WHERE decision_id = %s", (application_id,),
    )
    if now and not missing_terms(now[0]):
        return now[0]
    log.error(
        "could not repair incomplete offer application_id=%s missing=%s", application_id, ",".join(missing),
    )
    raise HTTPException(
        status_code=409,
        detail=(
            "This offer is incomplete and could not be regenerated -- it was accepted "
            "or its decision is no longer an approval. Escalate for manual remediation."
        ),
    )


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
    # One source of truth (fees.py). This is the CONTRACTUAL rate the payment
    # is calculated on, and it is persisted on the offer so boarding does not
    # have to infer it from the disclosed APR -- which is a different number.
    annual_rate = float(fees.NOTE_RATE_PCT)

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
            "INSERT INTO offers (app_id, decision_id, fee_pct_used, note_rate_pct, apr, "
            "finance_charge, monthly_payment, amount_financed, total_of_payments, "
            "regular_payment_count, final_payment, term_months, schedule_version) "
            "SELECT d.app_id, d.app_id, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s "
            "FROM decisions d WHERE d.app_id = %s AND d.outcome = 'approve' "
            "ON CONFLICT (decision_id) DO NOTHING "
            f"RETURNING {_OFFER_FIELDS}",
            (fee_pct_used, annual_rate, o["apr"], o["finance_charge"], o["monthly_payment"],
             o["amount_financed"], o["total_of_payments"],
             o["regular_payment_count"], o["final_payment"], body.term_months,
             fees.SCHEDULE_VERSION, body.application_id),
        )
    except psycopg2.errors.UniqueViolation:
        inserted = []
    created = bool(inserted)
    repaired = False
    if inserted:
        row = inserted[0]
    else:
        # Either no approved decision exists for this application_id, or an
        # offer already exists for it (ON CONFLICT DO NOTHING -- see above).
        # decisions.app_id is this offer's decision_id, so it's also
        # body.application_id here.
        existing = db.query(
            f"SELECT {_OFFER_FIELDS} FROM offers WHERE decision_id = %s",
            (body.application_id,),
        )
        if not existing:
            raise HTTPException(
                status_code=422,
                detail=f"no approved decision on record for application_id={body.application_id}",
            )
        row = existing[0]

        # Review fix: DO NOTHING + read-back returns the row that is ALREADY
        # there -- so for a pre-0026 incomplete row this endpoint handed the
        # same NULL terms straight back, and the float() coercions below turned
        # that into a 500. Migration 0026 told the operator to "regenerate the
        # offer from its decision", which this endpoint could not actually do.
        # It can now, for unaccepted offers only.
        missing = missing_terms(row)
        if missing:
            row = _repair_incomplete_offer(row, missing, o, fee_pct_used, body.application_id)
            repaired = True

    disclosure = Disclosure(
        note_rate_pct=(float(row["note_rate_pct"]) if row.get("note_rate_pct") is not None else None),
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
        created=created, repaired=repaired,
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

    # Gap F (PR #6 review): every one of the five canonical disclosure amounts
    # used to be read as `offer.<field> or <default>`. A NULL apr silently
    # became 7.99, a NULL finance_charge silently became 0 -- so a corrupt or
    # half-written offer row was rendered as a real, plausible-looking TILA
    # disclosure with invented terms, and the borrower could accept it. These
    # are canonical loan terms: if any is missing the row is not a disclosure,
    # and the honest answer is an explicit integrity error, not a default.
    missing = [name for name in CANONICAL_TERMS if getattr(offer, name) is None]
    if missing:
        log.error(
            "incomplete offer row offer_id=%s application_id=%s missing=%s",
            offer.id, application_id, ",".join(missing),
        )
        raise HTTPException(
            status_code=409,
            detail=(
                "This offer is incomplete and cannot be displayed. Missing required "
                f"disclosure terms: {', '.join(missing)}. POST /offers for this "
                "application to regenerate it (unaccepted offers only)."
            ),
        )

    # Rebuild the display schedule from the persisted offer (Offer ORM only). Recover the
    # principal/term from the stored disclosure box and reuse the stored APR as the schedule
    # rate — the same shortcut the LOS read path takes. Float math throughout (D1).
    monthly_payment = float(offer.monthly_payment)
    total_of_payments = float(offer.total_of_payments)
    amount_financed = float(offer.amount_financed)
    # W4 review fix: use the fee rule actually snapshotted on THIS row, not
    # whatever ORIGINATION_FEE_PCT happens to be right now -- reading the live
    # constant here instead of the stored snapshot was exactly the drift this
    # column exists to prevent (a fee-schedule change would silently change the
    # recovered principal, and therefore the redisplayed schedule, for every
    # existing offer).
    #
    # Review fix: the fallback for a legacy row with no snapshot used to read
    # that same live constant, which reintroduced the drift on exactly the rows
    # least able to absorb it. It now reads a frozen constant equal to what
    # db/migrations/0011 back-fills those rows with, so a legacy reconstruction
    # is deterministic and a fee-policy change cannot move it.
    if offer.fee_pct_used is not None:
        fee_pct = offer.fee_pct_used
    else:
        fee_pct = float(fees.LEGACY_PRE_SNAPSHOT_FEE_PCT)
        log.warning(
            "offer has no fee_pct_used snapshot; reconstructing with the frozen "
            "pre-snapshot rate offer_id=%s application_id=%s (db/migrations/0011 back-fill)",
            offer.id, application_id,
        )
    # Stored contractual facts first. These used to be INFERRED -- principal as
    # amount_financed / (1 - fee_pct), term as total_of_payments / monthly_payment
    # -- and the schedule regenerated by whatever generator was deployed at read
    # time, so an accepted disclosure could silently change meaning after a
    # rounding or fee change. Under Model B the final payment cannot be recovered
    # from any stored figure at all, which makes inference impossible rather than
    # merely unsafe.
    #
    # The legacy fallbacks below are reached ONLY for pre-0030 rows that never
    # stored these facts. They are never used for boarding: accept_offer requires
    # the stored terms and refuses otherwise.
    schedule_is_stored = offer.final_payment is not None and offer.term_months is not None
    if offer.term_months is not None:
        term_months = int(offer.term_months)
    else:
        term_months = round(total_of_payments / monthly_payment) if monthly_payment else 0
    principal = round(amount_financed / (1 - fee_pct), 2) if amount_financed else 0.0
    # Review fix: this used to build the schedule at `offer.apr`. The APR and
    # the note rate are not interchangeable once a prepaid fee exists -- the APR
    # is solved against the amount financed, the payments run on the full
    # principal -- so the redisplayed schedule showed a monthly payment that did
    # not match the disclosed one. Recover the rate the payments were actually
    # calculated at from the stored payment itself.
    # Prefer the STORED contractual rate. note_rate_from_payment() is legacy
    # compatibility ONLY -- for offers created before db/migrations/0030, which
    # have no stored value. A recovered rate is an inference from an already
    # rounded payment, so it must never be preferred over a persisted one, and
    # accept refuses to board a recovered rate at all (applications.py::
    # accept_offer). Asserted both ways:
    # test_stored_note_rate_is_preferred_over_recovery (normal rows never reach
    # the recovery) and test_a_legacy_offer_without_a_stored_note_rate_recovers.
    if offer.note_rate_pct is not None:
        note_rate = float(offer.note_rate_pct)
    elif term_months and monthly_payment:
        note_rate = apr.note_rate_from_payment(principal, monthly_payment, term_months)
    else:
        note_rate = 0.0
    rows = schedule.amortization(principal, note_rate, term_months) if term_months else []
    disclosure = Disclosure(
        note_rate_pct=(note_rate or None),
        apr=float(offer.apr), finance_charge=float(offer.finance_charge),
        monthly_payment=monthly_payment, amount_financed=amount_financed,
        total_of_payments=total_of_payments,
    )
    return OfferResponse(
        offer_id=offer.id, application_id=application_id,
        decision_id=offer.decision_id, fee_pct_used=fee_pct,
        apr=float(offer.apr), finance_charge=float(offer.finance_charge),
        monthly_payment=monthly_payment, total_of_payments=total_of_payments,
        disclosure=disclosure, schedule=[ScheduleRow(**r) for r in rows],
    )
