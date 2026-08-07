"""The Loan mapping must declare the Model B columns migration 0030 adds.

Nothing in this service reads them yet -- billing from the stored contractual
amounts, instead of regenerating the schedule at read time, is the remaining
half of that work. The columns are declared and asserted now anyway, because
the failure mode of forgetting is silent and has already cost this repository
two debugging sessions:

  * `pan` (PR #11): the ORM model stopped declaring it, so a `getattr` fallback
    written to display legacy last4 could never fire. The code read as correct.
  * origination's offer schedule columns (this PR): declared in SQL, not in the
    model, so fully populated offers reported their terms missing and could not
    be boarded. Caught by a browser test, not by 648 green unit tests.

In both cases the column existed in Postgres and the mapping did not, and a
`getattr` returned None with nothing raising. Declaring the column in the same
change as the migration is the cheap half of the fix; this test is the half
that stays.
"""
from app import models


def test_loan_maps_the_model_b_schedule_columns():
    """db/migrations/0030 adds these to `loans`; the mapping must know them.

    Asserted by name so that a reader who adds a column to the migration and
    not to the model gets told which one, rather than discovering it when a
    payment is billed against a None.
    """
    mapped = set(models.Loan.__mapper__.columns.keys())
    expected = {"regular_payment", "regular_payment_count", "final_payment",
                "schedule_version"}
    missing = sorted(expected - mapped)
    assert not missing, (
        "models.Loan does not declare " + ", ".join(missing) + ". These are "
        "written to `loans` at boarding (db/migrations/0030). An undeclared "
        "column reads as None through the ORM no matter what Postgres holds, "
        "so servicing would silently fall back to regenerating the schedule "
        "-- the drift this work exists to remove."
    )


def test_the_model_b_loan_columns_are_nullable():
    """Loans boarded before 0030 have no stored schedule, and never will.

    0030 does not back-fill: the terms of an already-funded loan are whatever
    was actually agreed, and solving them again today with the current
    generator would persist a reconstruction as if it were the original. A
    NOT NULL mapping would assert those loans cannot exist.
    """
    cols = models.Loan.__mapper__.columns
    for name in ("regular_payment", "regular_payment_count", "final_payment",
                 "schedule_version"):
        assert cols[name].nullable is True, f"loans.{name} is not back-filled"


def test_the_loan_schedule_columns_use_the_right_types():
    """A count is not money and a version is not a number."""
    cols = models.Loan.__mapper__.columns
    assert cols["regular_payment"].type.python_type is float
    assert cols["final_payment"].type.python_type is float
    assert cols["regular_payment_count"].type.python_type is int
    assert cols["schedule_version"].type.python_type is str
