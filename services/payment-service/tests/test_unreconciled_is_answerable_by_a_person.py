"""An alert nobody can act on is most of the way to no alert.

`payments_unapplied_count` and `payments_unapplied_exhausted_count` page
somebody when money has been captured on a card and never credited to a loan
balance. Until this change, the only thing that page could tell them was *how
many* -- `unreconciled_summary()` returns counts and a timestamp. Finding out
WHICH borrower had been charged without their balance moving meant opening
psql, because `GET /payments/unreconciled` was aggregates only and the gateway
proxied neither route, so nothing outside the compose network could ask at all.

WHAT IS ASSERTED HERE

  1. The listing names the affected payments, oldest first, and distinguishes a
     row still being retried from one that has stopped being retried -- the
     distinction that decides whether a human needs to do anything.
  2. It carries NO card data. Not the cardholder name, not `last4`, not the
     processor token or the authorization id. This response reaches a browser.
  3. A truncated listing can never be read as the whole of it.
  4. It reads. It does not retry, resolve, refund or reverse -- and this file
     asserts that against the database rather than against the docstring.

Real Postgres, on the same throwaway-schema pattern as
`test_reconcile_real_postgres.py`: what is being asserted is a query -- its
predicate, its ordering and its limit -- and a mocked cursor would assert none
of it.
"""
import importlib.util
import os
import pathlib

import psycopg2
import psycopg2.extras
import pytest

from app import db, reconcile
from app.config import RECONCILE_MAX_ATTEMPTS

#: `db/tests/real_schema.py`, loaded by path -- standard library only, so it
#: imports cleanly from a service test.
_REAL_SCHEMA_PATH = (pathlib.Path(__file__).resolve().parents[3]
                     / "db" / "tests" / "real_schema.py")
assert _REAL_SCHEMA_PATH.is_file(), (
    "expected the canonical schema helper at %s -- if it moved, this test must "
    "fail rather than fall back to a hand-written table" % _REAL_SCHEMA_PATH)
_spec = importlib.util.spec_from_file_location("meridian_real_schema",
                                               _REAL_SCHEMA_PATH)
real_schema = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_schema)

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against")

SCHEMA = "payment_unreconciled_view_test"


def _schema_sql():
    """`payments`, verbatim from `db/init`.

    RF-26. The first version of this file hand-wrote the table and said the card
    columns were "present ON PURPOSE even though nothing here reads them: a test
    whose fixture omits `last4` cannot prove that `last4` stays out of the
    response". That reasoning is right and is now free -- the real definition
    carries every column production has, including the ones this test needs to
    exist in order to prove their absence, and it carries them without anybody
    keeping a copy in step.
    """
    return real_schema.sql_for(SCHEMA, ["payments"])


@pytest.fixture
def pg(monkeypatch):
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(_schema_sql())
        # `payments.loan_id` REFERENCES `loans(id)` in production, which the
        # hand-written copy omitted.
        cur.execute(
            "INSERT INTO loans (id, applicant_name, principal, note_rate_pct, "
            "                   term_months) "
            "SELECT n, 'Fixture ' || n, 10000.00, 7.99, 48 "
            "  FROM generate_series(0, 100) AS n "
            "ON CONFLICT (id) DO NOTHING")
    monkeypatch.setattr(db, "_conn", conn, raising=False)
    yield conn
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.close()


def _rows(conn, sql, params=()):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else []


def _payment(conn, *, loan_id=1, amount="100.00", auth_status="captured",
             applied=False, attempts=0, age_minutes=0,
             correlation_id="pay_seededfortherow"):
    row = _rows(
        conn,
        "INSERT INTO payments (loan_id, amount, auth_status, applied_at, "
        "  apply_attempts, correlation_id, created_at, brand, last4, "
        "  authorization_id, processor_ref, capture_source, idempotency_key) "
        "VALUES (%s, %s, %s, %s, %s, %s, now() - (%s || ' minutes')::interval, "
        "        'visa', '4242', 'auth_abc123', 'ch_live_ref', 'processor', "
        "        'idem-key') "
        "RETURNING id",
        (loan_id, amount, auth_status,
         "2026-01-01T00:00:00+00:00" if applied else None,
         attempts, correlation_id, age_minutes),
    )
    return row[0]["id"]


# ---------------------------------------------------------------------------
# It names the payments, and says which ones need a person.
# ---------------------------------------------------------------------------

def test_the_listing_names_the_payments_the_summary_only_counted(pg):
    stuck = _payment(pg, loan_id=77, amount="250.00", age_minutes=30)

    result = reconcile.unreconciled_items()

    assert result["total"] == 1
    assert result["returned"] == 1
    assert result["truncated"] is False
    (item,) = result["items"]
    assert item["payment_id"] == stuck
    assert item["loan_id"] == 77
    assert item["amount"] == 250.00
    assert item["correlation_id"] == "pay_seededfortherow"


def test_a_row_still_being_retried_reads_differently_from_one_that_is_not(pg):
    """The distinction that decides whether anybody has to do something.

    A payment inside its retry budget will very likely fix itself. One that has
    exhausted it will not, and the difference is invisible in a count.
    """
    patient = _payment(pg, loan_id=1, attempts=0, age_minutes=5)
    needs_a_person = _payment(pg, loan_id=2, attempts=RECONCILE_MAX_ATTEMPTS,
                              age_minutes=10)

    by_id = {i["payment_id"]: i for i in reconcile.unreconciled_items()["items"]}

    assert by_id[patient]["exhausted"] is False
    assert by_id[needs_a_person]["exhausted"] is True


def test_the_oldest_is_first(pg):
    """The borrower most likely to have already called is at the top."""
    newest = _payment(pg, loan_id=1, age_minutes=1)
    oldest = _payment(pg, loan_id=2, age_minutes=600)
    middle = _payment(pg, loan_id=3, age_minutes=60)

    order = [i["payment_id"] for i in reconcile.unreconciled_items()["items"]]
    assert order == [oldest, middle, newest]


# ---------------------------------------------------------------------------
# What it must not show.
# ---------------------------------------------------------------------------

def test_no_card_data_reaches_the_listing(pg):
    """This response goes to a browser, so every field it omits cannot leak.

    The fixture writes a brand, the card's `last4` digits, an authorization
    id, a processor reference, a capture source and an idempotency key --
    every card-adjacent column production actually has -- precisely so their
    absence here means something.

    A cardholder NAME is not among them, and that is worth stating rather than
    silently dropping: the first version of this fixture wrote one, and moving
    to the real schema (RF-26) revealed that `payments` has no `name` column at
    all. The name is never persisted (D5d took it out of the log; nothing ever
    stored it), so "the listing does not return it" was a weaker claim than the
    truth, which is that there is nothing to return.
    `test_cardholder_name_not_logged` holds that line at the log, and
    `test_pan_cvv_never_enter_the_payment_path` at the intake end.
    """
    _payment(pg, loan_id=5)

    result = reconcile.unreconciled_items()
    blob = repr(result)

    for forbidden in ("4242", "visa", "auth_abc123", "ch_live_ref", "processor",
                      "idem-key"):
        assert forbidden not in blob, f"{forbidden!r} reached the operator listing"

    (item,) = result["items"]
    assert set(item) == {
        "payment_id", "loan_id", "amount", "created_at", "apply_attempts",
        "exhausted", "next_attempt_at", "correlation_id",
    }, sorted(item)


@pytest.mark.parametrize("auth_status,applied", [
    ("captured", True),     # already credited -- nothing wrong with it
    ("failed", False),      # declined -- no money was taken
    ("pending", False),     # not captured -- no money was taken
])
def test_only_money_that_actually_left_a_card_and_never_arrived_is_listed(
    pg, auth_status, applied
):
    """The predicate, at its edges.

    A listing that included declined or pending rows would report money in limbo
    that was never taken, and an operator who learned that once would stop
    trusting the screen.
    """
    _payment(pg, auth_status=auth_status, applied=applied)

    result = reconcile.unreconciled_items()
    assert result["items"] == []
    assert result["total"] == 0


# ---------------------------------------------------------------------------
# A truncated listing cannot be read as the whole of it.
# ---------------------------------------------------------------------------

def test_a_capped_listing_still_reports_the_true_total(pg):
    for n in range(5):
        _payment(pg, loan_id=n, age_minutes=n)

    result = reconcile.unreconciled_items(limit=2)

    assert result["returned"] == 2
    assert result["total"] == 5, (
        "the total came from the truncated list rather than from the table, so "
        "a capped screen would under-report money in limbo")
    assert result["truncated"] is True


@pytest.mark.parametrize("asked,expected", [
    (0, 1),                                        # clamped up
    (-5, 1),
    (10_000, reconcile.UNRECONCILED_LIST_LIMIT),   # clamped down
])
def test_the_limit_is_clamped_rather_than_obeyed(pg, asked, expected):
    """The caller may ask; it may not decide.

    Same rule the policy tool applies to `top_k`: a bound is enforced on the way
    out, not requested of whoever is calling.
    """
    for n in range(3):
        _payment(pg, loan_id=n, age_minutes=n)

    result = reconcile.unreconciled_items(limit=asked)
    assert result["returned"] == min(3, expected)


# ---------------------------------------------------------------------------
# It reads. It does not act.
# ---------------------------------------------------------------------------

def test_listing_changes_nothing_at_all(pg):
    """Asserted against the table, not against the docstring.

    Retrying is the drain's job and `POST /payments/reconcile` already triggers
    a pass by hand. There is no refund or reversal capability anywhere in this
    system, so a control here would be inventing one.
    """
    _payment(pg, loan_id=1, attempts=2, age_minutes=15)
    _payment(pg, loan_id=2, attempts=RECONCILE_MAX_ATTEMPTS, age_minutes=99)

    before = _rows(pg, "SELECT id, auth_status, applied_at, apply_attempts, "
                       "       apply_next_attempt_at, apply_last_error "
                       "  FROM payments ORDER BY id")
    reconcile.unreconciled_items()
    reconcile.unreconciled_items(limit=1)
    after = _rows(pg, "SELECT id, auth_status, applied_at, apply_attempts, "
                      "       apply_next_attempt_at, apply_last_error "
                      "  FROM payments ORDER BY id")

    assert before == after, "listing unreconciled payments modified them"


def test_the_summary_route_is_unchanged_by_this(pg):
    """The alerting shape stays exactly as it was.

    Something scrapes `unreconciled_summary()`; adding a listing must not change
    the response an alert already parses.
    """
    _payment(pg, loan_id=1, amount="10.00", age_minutes=3)

    summary = reconcile.unreconciled_summary()
    assert set(summary) == {"pending", "amount_pending", "exhausted",
                            "oldest_created_at"}
    assert summary["pending"] == 1
    assert summary["amount_pending"] == 10.00
