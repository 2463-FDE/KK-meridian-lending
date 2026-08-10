"""The ORM mapping must declare every offer field the code reads by name.

Why this file exists, specifically. `_offer_disclosure_or_none` and the
boarding gate read offer fields with `getattr(offer, field, None)` against a
SQLAlchemy row. An undeclared column is not an error there -- it reads as
None, exactly as a genuinely NULL column does. So omitting a column from
models.Offer produces a row that reports its terms as missing while Postgres
holds all of them, and nothing raises.

That is not hypothetical. It has now happened twice in this repository:

  * `note_rate_pct` (db/migrations/0030) -- fixed when the column was added.
  * the four Model B schedule columns, in this PR -- every freshly generated
    offer logged
    `missing=regular_payment_count,final_payment,term_months,schedule_version`
    against a fully populated row, which disabled Accept & board and failed
    the borrower-workflow e2e.

The whole-service unit suite was green for the entire second occurrence. It
could not have caught it: unit tests build offer rows as objects that carry
whatever attributes the test sets, so they never consult the ORM mapping at
all. Only a Postgres-backed read does -- which is why the failure first
appeared in e2e, the slowest and least specific place to find it.

These tests close that gap at unit speed and without a database: they assert
the field lists the router treats as canonical are all actually mapped. Adding
a canonical field without declaring its column now fails here, in under a
second, naming the missing column -- instead of in a browser test.

A companion real-Postgres test (test_accept_requires_offer_real_postgres.py)
covers the other half: that a populated row round-trips through this mapping
and is reported boardable.
"""
from app import models
from app.routers.applications import (
    BOARDING_REQUIRED_FIELDS,
    TILA_MONETARY_FIELDS,
)


def _mapped_offer_columns() -> set[str]:
    """Attribute names SQLAlchemy will actually populate from a row."""
    return set(models.Offer.__mapper__.columns.keys())


def test_boarding_required_fields_are_all_mapped_on_the_offer_model():
    """The exact defect above, stated as an assertion.

    Every name in BOARDING_REQUIRED_FIELDS is read via getattr() off an ORM
    row, so every one of them has to be a mapped column. A missing name here
    means offers whose terms are complete in SQL will be reported incomplete.
    """
    missing = sorted(set(BOARDING_REQUIRED_FIELDS) - _mapped_offer_columns())
    assert not missing, (
        "models.Offer does not declare "
        + ", ".join(missing)
        + ", but the boarding gate reads them with getattr(). Undeclared "
        "columns read as None regardless of what Postgres holds, so every "
        "offer would be reported as missing these terms and refused for "
        "boarding. Declare the column(s) in services/origination-service/"
        "app/models.py."
    )


def test_tila_monetary_fields_are_all_mapped_on_the_offer_model():
    """The display gate reads these off the same row, with the same hazard.

    Kept separate from the boarding assertion above rather than folded into
    it: the two lists answer different questions and are allowed to diverge,
    so a regression in either should name which one broke.
    """
    missing = sorted(set(TILA_MONETARY_FIELDS) - _mapped_offer_columns())
    assert not missing, (
        "models.Offer does not declare "
        + ", ".join(missing)
        + ", but _offer_disclosure_or_none reads them with getattr(). A real "
        "disclosure would render as 'no offer'."
    )


def test_the_offer_model_maps_the_schedule_columns_with_usable_types():
    """A declared column of the wrong type is the same bug with a nicer log.

    `regular_payment_count` is a count and `schedule_version` an identifier;
    declaring either as money would round or reformat the value in transit.
    Asserted through the mapper rather than by reading models.py, so this
    reflects what SQLAlchemy resolved, not what the source appears to say.
    """
    cols = models.Offer.__mapper__.columns
    assert cols["regular_payment_count"].type.python_type is int
    assert cols["term_months"].type.python_type is int
    assert cols["schedule_version"].type.python_type is str
    # asdecimal=False across this codebase's money columns (D12) -- float here
    # is the deliberate boundary choice, not an oversight.
    assert cols["final_payment"].type.python_type is float


def test_every_mapped_schedule_column_is_nullable():
    """Migration 0030 adds these without a back-fill, so existing rows are NULL.

    A NOT NULL mapping would make SQLAlchemy's own metadata disagree with the
    deployed schema, and would imply legacy offers are impossible -- the very
    rows the display/boarding split exists to handle.
    """
    cols = models.Offer.__mapper__.columns
    for name in ("regular_payment_count", "final_payment", "term_months",
                 "schedule_version", "note_rate_pct"):
        assert cols[name].nullable is True, (
            f"offers.{name} is populated by migration 0030 without a "
            "back-fill, so pre-0030 rows hold NULL"
        )


def test_the_los_forwards_schedule_provenance():
    """The LOS must not drop the estimate label on the way to the borrower.

    disclosure-service reports whether the rows came from the stored contract or
    were reconstructed; dropping it here is what let an estimate reach the
    borrower page looking exactly like a contract. Reviewed on PR #10.
    """
    from app.routers.offers import _to_offer_out

    resp = {
        "offer_id": 1, "application_id": 1, "decision_id": 1, "fee_pct_used": 0.03,
        "apr": 10.072, "finance_charge": 2369.15, "monthly_payment": 469.98,
        "total_of_payments": 16919.15,
        "schedule": [],
        "schedule_source": "reconstructed",
        "schedule_note": "This payment schedule is an estimate.",
        "disclosure": {
            "note_rate_pct": None, "apr": 10.072, "finance_charge": 2369.15,
            "monthly_payment": 469.98, "amount_financed": 14550.0,
            "total_of_payments": 16919.15,
        },
    }
    out = _to_offer_out(1, resp)
    assert out.disclosure.schedule_source == "reconstructed"
    assert out.disclosure.schedule_note == "This payment schedule is an estimate."


def test_the_los_forwards_a_contract_label_unchanged():
    from app.routers.offers import _to_offer_out

    resp = {
        "offer_id": 1, "application_id": 1, "decision_id": 1, "fee_pct_used": 0.03,
        "apr": 13.51, "finance_charge": 202.03, "monthly_payment": 24.47,
        "total_of_payments": 1174.46, "schedule": [],
        "schedule_source": "contract", "schedule_note": None,
        "disclosure": {
            "note_rate_pct": 7.99, "apr": 13.51, "finance_charge": 202.03,
            "monthly_payment": 24.47, "amount_financed": 972.43,
            "total_of_payments": 1174.46,
        },
    }
    out = _to_offer_out(1, resp)
    assert out.disclosure.schedule_source == "contract"
    assert out.disclosure.schedule_note is None
