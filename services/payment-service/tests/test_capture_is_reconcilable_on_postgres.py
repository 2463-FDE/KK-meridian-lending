"""A real charge must land as a row reconciliation will actually compare.

The reported defect. Migration 0042 scopes reconciliation's ledger side to
`capture_source = 'processor'`, because `payments` has a second writer --
servicing-service's legacy `POST /payments` -- whose rows no settlement file can
contain. The capture UPDATE wrote `authorization_id`, `captured_at` and
`processor_ref` and **never set `capture_source`**, so every newly captured
payment kept the column default of `'unknown'` and servicing's filter dropped it.

The control would then have compared a settlement file against a ledger side the
filter had emptied, found nothing to disagree with, and reported `ok`. That is
the vacuous success this entire change exists to prevent, arriving through the
one column that decides what gets compared -- and every other test in this branch
would still have passed, because each of them writes the row it then reads.

So this runs the real `charge()` against the real schema and inspects what
PostgreSQL actually holds afterwards. Only the processor (stubbed, as in
dev/test) and the HTTP call to servicing are faked; the SQL is not.

**The seam this cannot cross, stated rather than implied.** servicing-service's
reconciliation module cannot be imported here -- both services expose a top-level
package named `app`, and loading the second over the first would test neither. So
the loop is closed in two halves that are bound mechanically rather than by
hope: this file asserts what payment-service WRITES, and
`servicing-service/tests/test_reconciliation_transaction_level_on_postgres.py`
asserts that a row of exactly this shape is COMPARED, plus that servicing's
filter value is the value payment-service's source writes.
"""
import os
import pathlib

import psycopg2
import psycopg2.extras
import pytest

from app import db, payments, processor

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against"
)

REPO = pathlib.Path(__file__).resolve().parents[3]
SCHEMA = "payment_capture_scope_test"

_VALID_MOCK_TOKEN = "tok_mock_550e8400-e29b-41d4-a716-446655440000"


@pytest.fixture
def pg(monkeypatch):
    """The REAL schema, not a hand-written subset.

    A local CREATE TABLE listing the columns this test expects would pass even if
    001_schema.sql had never gained `capture_source` -- which is precisely the
    class of defect being tested.
    """
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute((REPO / "db" / "init" / "001_schema.sql").read_text(encoding="utf-8"))
        cur.execute(
            "INSERT INTO loans (id, applicant_name, principal, apr, term_months) "
            "VALUES (4471, 'Sam Okafor', 9000, 5.946, 24)"
        )
        cur.execute("INSERT INTO balances (loan_id, balance) VALUES (4471, 1000.00)")
    conn.autocommit = True

    scoped = psycopg2.connect(DATABASE_URL)
    scoped.autocommit = True
    with scoped.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
    monkeypatch.setattr(db, "_conn", scoped, raising=False)

    # Only the boundaries: the processor's authorization and the HTTP hop to
    # servicing. Everything between them is the real code path.
    monkeypatch.setattr(payments, "_require_servicing_auth", lambda *a, **k: None)
    monkeypatch.setattr(payments, "_apply_via_servicing", lambda *a, **k: True)
    processor._stub_authorizations.clear()

    yield conn
    processor._stub_authorizations.clear()
    scoped.close()
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.close()


def _row(conn, payment_id):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(
            "SELECT auth_status, authorization_id, captured_at, processor_ref, "
            "capture_source FROM payments WHERE id = %s", (payment_id,)
        )
        return cur.fetchone()


def _charge(key="e2e-key-1"):
    return payments.charge(
        loan_id=4471, processor_token=_VALID_MOCK_TOKEN, last4="4242",
        brand="visa", amount=250.00, idempotency_key=key,
    )


def test_a_real_charge_is_marked_in_scope_for_reconciliation(pg):
    """The reported defect, against the real column and its real default."""
    result = _charge()

    row = _row(pg, result["payment_id"])
    assert row["auth_status"] == "captured"
    assert row["capture_source"] == "processor", (
        "the captured row kept db/migrations/0042's default of "
        f"{row['capture_source']!r}. servicing reconciliation filters its ledger "
        "side on capture_source = 'processor', so this payment is excluded from "
        "the comparison -- and a run that compares a settlement file against a "
        "ledger side the filter emptied reports ok"
    )


def test_a_real_charge_carries_the_settlement_join_key(pg):
    result = _charge()

    row = _row(pg, result["payment_id"])
    assert row["processor_ref"], (
        "the captured row has no processor_ref, so it can be matched to no "
        "settlement line and reconciliation reports it as unreferenced"
    )
    assert row["captured_at"] is not None, (
        "the captured row records that money moved but not when, so the "
        "reconciliation window cannot place it"
    )
    assert row["authorization_id"]


def test_a_recovered_capture_lands_in_scope_too(pg):
    """The crash-recovery path is a second capture UPDATE, and a fix applied to
    one statement and not the other is how this defect arrived in the first
    place: `captured_at` was correct on the recovery path and wrong on the happy
    one for a whole review round."""
    # First attempt: the processor approves, then the capture UPDATE dies.
    real_query = db.query

    def _crash_on_capture(sql, params=None):
        if sql.strip().startswith("UPDATE") and "auth_status = 'captured'" in sql:
            raise RuntimeError("simulated crash before the capture was persisted")
        return real_query(sql, params)

    db.query = _crash_on_capture
    try:
        with pytest.raises(RuntimeError):
            _charge("e2e-recovery")
    finally:
        db.query = real_query

    with pg.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT id, auth_status FROM payments WHERE idempotency_key = %s",
                    ("e2e-recovery",))
        payment_id, status = cur.fetchone()
    assert status == "pending", "this test no longer reproduces the crash it claims to"

    # The retry recovers it from the processor's own record of the key.
    _charge("e2e-recovery")

    row = _row(pg, payment_id)
    assert row["auth_status"] == "captured"
    assert row["capture_source"] == "processor"
    assert row["processor_ref"]


def test_the_column_default_really_is_the_excluding_one(pg):
    """Guard the guard.

    If the default were 'processor', both tests above would pass without the
    application setting anything -- and the legacy servicing writer would be
    swept back into the comparison, which is the defect 0042 exists to fix.
    """
    with pg.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(
            "INSERT INTO payments (loan_id, amount, method) "
            "VALUES (4471, 10.00, 'card') RETURNING capture_source"
        )
        assert cur.fetchone()[0] == "unknown"
