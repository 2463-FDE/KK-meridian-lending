"""The Model B schedule constraints from db/init/001_schema.sql, proven to reject.

A CHECK constraint that exists is not the same as a CHECK constraint that
works. test_migration_paths_converge already asserts both provisioning paths
declare the same constraints by name and normalized expression -- that proves
they AGREE, not that either one refuses anything. These tests write the
offending rows.

Each rule is a fact about what a Model B schedule is, so each gets a row that
violates exactly it:

  * a schedule is all four columns or none of them -- a subset describes
    nothing, while still reading as "recorded" to a single-column NULL check;
  * there are term_months - 1 regular payments and one adjusted final payment,
    so the count and the term cannot disagree;
  * a billed amount of zero or less is not a payment;
  * an unrecognised schedule_version is a row whose amounts were produced by
    rounding rules this codebase does not have.

Why the database and not just the application: the application is one writer
among several. Seed SQL, the offer repair path, migrations and any operator
with psql all write these tables, and the application-level checks protect
none of those callers.

Built from db/init (a fresh volume) rather than by replaying migrations. The
constraints are identical on both paths -- that is what the parity suite is
for -- and db/init is the shorter path to a table to insert into.
"""
import os
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against")

INIT_DIR = Path(__file__).resolve().parent.parent / "init"
SCHEMA = "constraint_test_0030"

# A self-consistent Model B offer: 24 months means 23 regular payments plus one
# adjusted final payment. Deliberately internally consistent so that every
# failure below is caused by the single field the test changes, not by a fixture
# that was already invalid.
_VALID_OFFER = {
    "app_id": 1,
    "note_rate_pct": 7.990,
    "apr": 11.029,
    "finance_charge": 768.11,
    "monthly_payment": 407.00,
    "amount_financed": 8730.00,
    "total_of_payments": 9768.11,
    "regular_payment_count": 23,
    "final_payment": 407.12,
    "term_months": 24,
    "schedule_version": "B1",
    # `principal` joined the all-or-nothing set on PR #10: expanding a stored
    # schedule needs the principal the payments run on, so a row with the other
    # five and no principal is a schedule that cannot be reproduced. Without it
    # here every test below would be rejected by the all-or-nothing rule before
    # reaching the rule it means to exercise -- which is the fixture-already-
    # invalid trap this block's own comment warns about.
    "principal": 9000.00,
}

_VALID_LOAN = {
    "app_id": 1,
    "applicant_name": "Robin Fictional",
    "principal": 9000.00,
    "note_rate_pct": 7.990,
    "term_months": 24,
    "regular_payment": 407.00,
    "regular_payment_count": 23,
    "final_payment": 407.12,
    "schedule_version": "B1",
}


@pytest.fixture
def conn():
    connection = psycopg2.connect(DATABASE_URL)
    connection.autocommit = False
    with connection.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(f"SET search_path TO {SCHEMA}")
        # 001 alone: offers/loans and the applications+decisions rows they
        # reference all live there. 002/003 are seed DATA.
        cur.execute((INIT_DIR / "001_schema.sql").read_text())
        cur.execute(
            "INSERT INTO applicants (id, name) VALUES (1, 'Robin Fictional');"
            "INSERT INTO applications (id, applicant_id, amount, term_months, status) "
            "VALUES (1, 1, 9000, 24, 'approved');"
            "INSERT INTO decisions (app_id, outcome) VALUES (1, 'approve');"
        )
    connection.commit()
    yield connection
    with connection.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    connection.commit()
    connection.close()


def _insert(conn, table, row):
    """Insert a dict, letting any constraint violation propagate."""
    cols = ", ".join(row)
    marks = ", ".join(["%s"] * len(row))
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(f"INSERT INTO {table} ({cols}) VALUES ({marks})", tuple(row.values()))
    conn.commit()


def _rejects(conn, table, row, constraint):
    """Assert the insert is refused by a NAMED constraint.

    Matching the name matters: an insert that fails for some unrelated reason
    (a NOT NULL, a bad type, a typo in the test) would otherwise read as proof
    that the rule under test works.
    """
    with pytest.raises(psycopg2.errors.CheckViolation) as exc:
        _insert(conn, table, row)
    conn.rollback()
    assert constraint in str(exc.value), (
        f"row was rejected, but by {exc.value.diag.constraint_name!r} rather "
        f"than {constraint!r} -- the intended rule may not be enforced at all"
    )


# --------------------------------------------------------------------------
# offers
# --------------------------------------------------------------------------

def test_a_fully_populated_offer_schedule_is_accepted(conn):
    """The control. Without it, every rejection below could be a fixture that
    no valid row can satisfy."""
    _insert(conn, "offers", dict(_VALID_OFFER))
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT count(*)::int AS n FROM offers")
        assert cur.fetchone()["n"] == 1


def test_a_legacy_offer_with_no_schedule_at_all_is_accepted(conn):
    """0030 does not back-fill, so all-NULL is the normal state of every row
    that predates it. A constraint that rejected these would make the
    migration undeployable."""
    # All SIX, since principal and note_rate_pct joined the set on PR #10. A
    # pre-0030 row has none of them: the columns did not exist yet.
    row = {k: v for k, v in _VALID_OFFER.items()
           if k not in ("regular_payment_count", "final_payment",
                        "term_months", "schedule_version", "principal",
                        "note_rate_pct")}
    _insert(conn, "offers", row)


@pytest.mark.parametrize("omitted", [
    "regular_payment_count", "final_payment", "term_months", "schedule_version",
    # Both added on PR #10. `principal` was the reachable gap in production:
    # the repair path could not fill it and the read path inverted
    # amount_financed through the fee instead, landing on a neighbouring
    # principal and displaying the result as the agreed schedule.
    "principal", "note_rate_pct",
])
def test_a_partly_recorded_offer_schedule_is_rejected(conn, omitted):
    """One case per column: no single field can quietly become optional.

    This is the rule the rest of the codebase depends on. Several call sites
    test one column and conclude about the group; that inference is only sound
    because a proper subset cannot exist.
    """
    row = dict(_VALID_OFFER)
    row[omitted] = None
    if omitted == "term_months":
        # Nulling term_months alone would ALSO violate the count/term identity,
        # and the two constraints would race to report first. Drop the count
        # too so the all-or-nothing rule is unambiguously the one under test.
        row["regular_payment_count"] = None
        _rejects(conn, "offers", row, "offers_schedule_all_or_nothing")
        return
    _rejects(conn, "offers", row, "offers_schedule_all_or_nothing")


def test_a_count_that_disagrees_with_the_term_is_rejected(conn):
    """The corruption a mismatched request body used to produce: a 36-month
    schedule filed under a 60-month term."""
    row = dict(_VALID_OFFER, regular_payment_count=35, term_months=60)
    _rejects(conn, "offers", row, "offers_schedule_term_agrees")


def test_an_off_by_one_count_is_rejected(conn):
    """term_months regular payments plus a final payment is term_months + 1
    payments -- one more than the borrower agreed to. The likeliest way to get
    this wrong is also the smallest error, so it gets its own case."""
    row = dict(_VALID_OFFER, regular_payment_count=24, term_months=24)
    _rejects(conn, "offers", row, "offers_schedule_term_agrees")


def test_a_single_payment_schedule_is_accepted(conn):
    """A one-period loan is all final payment: zero regular payments, and
    0 + 1 = 1 satisfies the identity. Asserted because a `> 0` written where
    `>= 0` belongs would forbid a legitimate schedule."""
    _insert(conn, "offers", dict(_VALID_OFFER, regular_payment_count=0, term_months=1))


def test_a_zero_term_is_rejected(conn):
    """A schedule with no periods bills nothing. Its count would have to be
    -1 to satisfy the identity, so this is caught by shape, not by arithmetic."""
    row = dict(_VALID_OFFER, regular_payment_count=-1, term_months=0)
    _rejects(conn, "offers", row, "offers_schedule_shape_sane")


@pytest.mark.parametrize("amount", [0, -407.12])
def test_a_non_positive_final_payment_is_rejected(conn, amount):
    """Zero and negative both: zero is the value an uninitialised numeric
    column takes, and negative is what a sign error produces."""
    _rejects(conn, "offers", dict(_VALID_OFFER, final_payment=amount),
             "offers_final_payment_positive")


@pytest.mark.parametrize("version", ["B2", "A1", "b1", "", "Model B"])
def test_an_unsupported_schedule_version_is_rejected(conn, version):
    """Including 'b1': the version selects a rounding policy, and a
    case-insensitive match would let two spellings mean the same thing until
    one of them didn't.

    'B2' is the case that matters operationally -- a future policy must add its
    version to this constraint deliberately, in a migration, rather than
    appearing in the data first and being noticed later.
    """
    _rejects(conn, "offers", dict(_VALID_OFFER, schedule_version=version),
             "offers_schedule_version_supported")


# --------------------------------------------------------------------------
# loans -- the same rules on the boarded contract
# --------------------------------------------------------------------------

def test_a_fully_populated_loan_schedule_is_accepted(conn):
    _insert(conn, "loans", dict(_VALID_LOAN))


def test_a_legacy_loan_with_no_stored_schedule_is_accepted(conn):
    """Loans boarded before 0030 have no stored schedule and never will --
    reconstructing one would persist a guess as the agreed terms."""
    row = {k: v for k, v in _VALID_LOAN.items()
           if k not in ("regular_payment", "regular_payment_count",
                        "final_payment", "schedule_version")}
    _insert(conn, "loans", row)


@pytest.mark.parametrize("omitted", [
    "regular_payment", "regular_payment_count", "final_payment", "schedule_version",
])
def test_a_partly_recorded_loan_schedule_is_rejected(conn, omitted):
    _rejects(conn, "loans", dict(_VALID_LOAN, **{omitted: None}),
             "loans_schedule_all_or_nothing")


def test_a_loan_count_that_disagrees_with_its_own_term_is_rejected(conn):
    """loans.term_months already existed and is NOT NULL, so the identity is
    checked against the loan's own term rather than a copied one -- a boarding
    bug that copied the wrong term cannot satisfy both."""
    _rejects(conn, "loans", dict(_VALID_LOAN, regular_payment_count=35),
             "loans_schedule_term_agrees")


@pytest.mark.parametrize("field", ["regular_payment", "final_payment"])
def test_a_non_positive_loan_payment_is_rejected(conn, field):
    _rejects(conn, "loans", dict(_VALID_LOAN, **{field: 0}),
             "loans_schedule_amounts_positive")


def test_an_unsupported_loan_schedule_version_is_rejected(conn):
    _rejects(conn, "loans", dict(_VALID_LOAN, schedule_version="B2"),
             "loans_schedule_version_supported")


def test_a_single_payment_loan_is_accepted(conn):
    _insert(conn, "loans", dict(_VALID_LOAN, regular_payment_count=0, term_months=1))
