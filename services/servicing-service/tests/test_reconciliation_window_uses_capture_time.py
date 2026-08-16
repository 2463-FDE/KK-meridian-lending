"""A capture that crosses midnight must not be reported as a break.

Reconciliation scoped its ledger side by `payments.created_at`. That column is
stamped at INSERT, while the row is still `auth_status = 'pending'` and before
the processor has been called; the flip to 'captured' happens afterwards. An
authorization that is slow, retried, or recovered after a crash can therefore be
created on one day and captured on the next.

So a capture the processor settles on the 9th could carry a `created_at` of the
8th. The 9th's settlement file was compared against a ledger window that
excluded it, and the loan was reported as a money break with nothing actually
wrong.

That is the worst kind of false positive for a money control. A reviewer who
learns the breaks are usually spurious stops reading them, and a control nobody
believes has stopped working -- which is the same D7 failure this PR exists to
close, arriving by a different route.

Real PostgreSQL, because the assertion is about a SQL window predicate and two
timestamps on the same row.
"""
import datetime as dt
import os
import pathlib

import psycopg2
import pytest

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against"
)

REPO = pathlib.Path(__file__).resolve().parents[3]
SCHEMA = "reconciliation_capture_window_test"

# The payment is created at 23:52 on the 8th and captured at 00:04 on the 9th.
CREATED = dt.datetime(2026, 8, 8, 23, 52, tzinfo=dt.timezone.utc)
CAPTURED = dt.datetime(2026, 8, 9, 0, 4, tzinfo=dt.timezone.utc)
SETTLEMENT_DAY = "2026-08-09"


@pytest.fixture
def db(monkeypatch):
    from app import reconciliation

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute((REPO / "db" / "init" / "001_schema.sql").read_text(encoding="utf-8"))
        cur.execute(
            "INSERT INTO loans (id, applicant_name, principal, note_rate_pct, term_months) "
            "VALUES (4471, 'Sam Okafor', 9000, 5.946, 24)"
        )
        cur.execute(
            "INSERT INTO payments (loan_id, amount, method, auth_status, created_at, "
            "captured_at, capture_source) "
            "VALUES (4471, 250.00, 'card', 'captured', %s, %s, 'processor')",
            (CREATED, CAPTURED),
        )

    scoped = f"{DATABASE_URL}?options=-csearch_path%3D{SCHEMA}"
    monkeypatch.setattr(reconciliation.db, "DATABASE_URL", scoped, raising=False)
    monkeypatch.setattr(reconciliation.db, "_conn", None, raising=False)
    yield conn
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.close()
    monkeypatch.setattr(reconciliation.db, "_conn", None, raising=False)


def test_a_capture_after_midnight_is_in_the_settlement_day_window(db):
    """The reported defect. The processor settles it on the 9th; so must we."""
    from app import reconciliation

    totals = reconciliation._ledger_by_loan((SETTLEMENT_DAY, SETTLEMENT_DAY))
    assert 4471 in totals, (
        "a payment captured at 00:04 on the settlement day is missing from the "
        "ledger side of that day's comparison -- it would be reported as a "
        "money break with nothing actually wrong"
    )
    assert str(totals[4471]) == "250.00"


def test_it_is_not_also_counted_in_the_previous_day(db):
    """The other half. Counting it on the 8th as well would double-count it and
    produce a break on the day it was created."""
    from app import reconciliation

    totals = reconciliation._ledger_by_loan(("2026-08-08", "2026-08-08"))
    assert 4471 not in totals, (
        "the capture is counted in the day the row was CREATED as well, so the "
        "8th now shows money the processor settled on the 9th"
    )


def test_a_row_with_no_captured_at_still_falls_back_to_created_at(db):
    """Rows captured before migration 0040 were back-filled, but a row that
    escaped the back-fill must still be compared.

    Dropping it would understate our total and produce a break in the other
    direction. A missing row is not a safe default for a money control.
    """
    from app import reconciliation

    with db.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("UPDATE payments SET captured_at = NULL WHERE loan_id = 4471")

    totals = reconciliation._ledger_by_loan(("2026-08-08", "2026-08-08"))
    assert 4471 in totals, (
        "a legacy row with no captured_at vanished from the comparison entirely"
    )


def _capture_statements():
    """The text of every capturing `db.query(...)` call in payments.py.

    Matched on the call's own parentheses rather than by splitting the file on
    ';'. The prose around that code contains semicolons, so a split version cut
    the statement in half and then satisfied itself with the column name it found
    in a COMMENT. A guard that passes on documentation is not a guard.
    """
    source = (REPO / "services" / "payment-service" / "app" / "payments.py").read_text(
        encoding="utf-8"
    )
    calls = []
    marker = "db.query("
    at = source.find(marker)
    while at != -1:
        depth, i = 0, at + len(marker) - 1
        while i < len(source):
            if source[i] == "(":
                depth += 1
            elif source[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        calls.append(source[at:i + 1])
        at = source.find(marker, i)
    return [c for c in calls if "auth_status = 'captured'" in c]


def test_the_capture_timestamp_is_written_with_the_status(db):
    """`captured_at` must never be set separately from `auth_status`.

    A captured row without the timestamp is a row reconciliation has to guess
    about, and the fallback above exists only for rows that predate the column.
    """
    statements = _capture_statements()
    assert statements, "no capture UPDATE found -- has the write path moved?"
    for stmt in statements:
        assert "captured_at = COALESCE(" in stmt, (
            "a capture UPDATE sets auth_status without taking captured_at from "
            "the processor, so the row records that it was captured but not "
            "when the processor says it happened"
        )
