"""PR #11 review: prove the pan/cvv removal is a safe two-release rollout.

The review's finding was that dropping the columns in the same release as the
code that stops reading them takes payment history down for any instance that
has not restarted yet -- servicing's `_display_last4` reads `payment.pan` on
`main` today. Correct, so the change was split:

    0029  EXPAND    back-fill last4 from pan. Columns stay.
    0031  CONTRACT  drop pan and cvv.

The property that makes the split worth anything is the **overlap window**:
after 0029 and before 0031, the database must satisfy BOTH the old code and the
new code, so instances can restart in any order. That is what these tests
assert, against real PostgreSQL, rather than the ordering being a claim in a PR
description.

The two migrations are applied here directly rather than through the whole
chain, so a failure names the rollout property that broke instead of surfacing
as some unrelated migration erroring.
"""
import os
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against")

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
EXPAND = MIGRATIONS / "0029_payments_backfill_last4.sql"
CONTRACT = MIGRATIONS / "0031_drop_payments_pan_cvv.sql"

SCHEMA = "expand_contract_test"

# The pre-tokenization shape: a card row with a full PAN and no last4, which is
# exactly the row the back-fill has to rescue.
_LEGACY_SETUP = f"""
    SET search_path TO {SCHEMA};
    CREATE TABLE payments (
        id SERIAL PRIMARY KEY,
        loan_id INTEGER,
        pan TEXT,
        cvv TEXT,
        last4 TEXT,
        brand TEXT,
        amount NUMERIC(14,2) NOT NULL,
        method TEXT DEFAULT 'card'
    );
    INSERT INTO payments (loan_id, pan, cvv, amount, method) VALUES
        (1, '4111111111111111', '123', 100, 'card'),
        (2, '340000000000009', '4021', 50, 'card');
    INSERT INTO payments (loan_id, last4, brand, amount, method) VALUES
        (3, '4242', 'visa', 75, 'card');
    INSERT INTO payments (loan_id, amount, method) VALUES
        (4, 432.18, 'ach');
"""


def _run(conn, sql):
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        # 0031 refuses to run without this acknowledgement -- it destroys data
        # and can break servicing instances still reading payments.pan. A test
        # harness IS the operator here, so it acknowledges explicitly rather
        # than the gate being weakened to let automation through. Set on every
        # statement because a GUC set with SET is session-scoped and these
        # helpers do not assume one long-lived session.
        cur.execute("SET meridian.pan_drop_acknowledged = 'yes'")
        cur.execute(sql)
    conn.commit()


def _rows(conn, sql):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(sql)
        return cur.fetchall()


def _columns(conn):
    return {
        r["column_name"]
        for r in _rows(
            conn,
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_schema = '{SCHEMA}' AND table_name = 'payments'",
        )
    }


@pytest.fixture
def legacy_db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
    conn.commit()
    _run(conn, _LEGACY_SETUP)
    yield conn
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.commit()
    conn.close()


# --- the overlap window -------------------------------------------------------

def test_after_expand_the_old_columns_are_still_there(legacy_db):
    """The whole reason for splitting. An instance that has not restarted still
    SELECTs pan and cvv; if 0029 removed them, that instance 500s."""
    _run(legacy_db, EXPAND.read_text())

    cols = _columns(legacy_db)
    assert "pan" in cols, "0029 must not drop pan -- old instances still read it"
    assert "cvv" in cols, "0029 must not drop cvv"


def test_after_expand_the_new_code_needs_nothing_but_last4(legacy_db):
    """The other half of the overlap: a restarted instance reads only last4, so
    every card row must already have one."""
    _run(legacy_db, EXPAND.read_text())

    blank = _rows(
        legacy_db,
        "SELECT id FROM payments WHERE method = 'card' AND last4 IS NULL",
    )
    assert blank == [], f"card rows with no last4 after the back-fill: {blank}"

    # And the recovered digits are the right ones, not just non-null.
    by_loan = {r["loan_id"]: r["last4"] for r in _rows(legacy_db, "SELECT loan_id, last4 FROM payments")}
    assert by_loan[1] == "1111"
    assert by_loan[2] == "0009"
    assert by_loan[3] == "4242"      # already had one; untouched
    assert by_loan[4] is None        # ACH, never had a card


def test_contract_only_removes_what_expand_made_redundant(legacy_db):
    _run(legacy_db, EXPAND.read_text())
    _run(legacy_db, CONTRACT.read_text())

    cols = _columns(legacy_db)
    assert "pan" not in cols
    assert "cvv" not in cols
    # Display data survives the drop -- that is what the back-fill bought.
    assert {r["last4"] for r in _rows(legacy_db, "SELECT last4 FROM payments WHERE method = 'card'")} == {
        "1111", "0009", "4242",
    }


def test_no_card_data_survives_the_contract_step(legacy_db):
    """The point of the exercise. After 0031 there is no column anywhere in
    payments still holding a full card number or a security code."""
    _run(legacy_db, EXPAND.read_text())
    _run(legacy_db, CONTRACT.read_text())

    text_cols = [
        r["column_name"]
        for r in _rows(
            legacy_db,
            "SELECT column_name FROM information_schema.columns "
            f"WHERE table_schema = '{SCHEMA}' AND table_name = 'payments' "
            "AND data_type = 'text'",
        )
    ]
    for col in text_cols:
        long_values = _rows(
            legacy_db,
            f"SELECT count(*)::int AS n FROM payments WHERE {col} ~ '^[0-9]{{12,19}}$'",
        )
        assert long_values[0]["n"] == 0, f"column {col} still holds card-length digits"


# --- both migrations replay --------------------------------------------------

def test_both_steps_are_idempotent(legacy_db):
    """The parity suite replays every migration twice; these two must survive
    that, including 0029's back-fill referencing a column 0031 has removed."""
    for _ in range(2):
        _run(legacy_db, EXPAND.read_text())
    for _ in range(2):
        _run(legacy_db, CONTRACT.read_text())
    # And expand again, now that pan is gone -- the ordering a re-run hits.
    _run(legacy_db, EXPAND.read_text())

    assert "pan" not in _columns(legacy_db)
    assert {r["last4"] for r in _rows(legacy_db, "SELECT last4 FROM payments WHERE method = 'card'")} == {
        "1111", "0009", "4242",
    }
