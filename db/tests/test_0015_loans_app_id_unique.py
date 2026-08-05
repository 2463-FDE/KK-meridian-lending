"""Integration test for db/migrations/0015_accept_token_and_loans_app_id_unique.sql.

Review finding: 0015 added `UNIQUE (app_id)` to loans with no duplicate
cleanup at all, even though this branch's own commit history documents a
real race (two concurrent accept_offer calls both boarding a loan for the
same application). Any environment that actually hit that race blocks on
deploy -- the ALTER TABLE fails on exactly the rows it exists to guard
against.

Unlike 0011's offers cleanup, a loan has child rows (balances is a 1:1 FK on
loan_id, payments is a plain FK) -- deleting a losing duplicate loan outright
would violate those FKs. This tests the full three-part cleanup: a payment
already recorded against a duplicate is reassigned (never dropped -- it's a
real money-movement record), the loser's balances row is dropped, then the
loser loan row itself, before the constraint is added.

Runs against a real Postgres (DATABASE_URL) rather than mocking SQL, since
the bug is in migration-time data cleanup, not application code. Skips if
DATABASE_URL isn't set; the CI db-migrations job always sets it.
"""
import os
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
SCHEMA = "migration_test_0015"


@pytest.fixture
def conn():
    connection = psycopg2.connect(DATABASE_URL)
    connection.autocommit = False
    cur = connection.cursor()
    cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    cur.execute(f"CREATE SCHEMA {SCHEMA}")
    cur.execute(f"SET search_path TO {SCHEMA}")
    connection.commit()
    yield connection
    cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    connection.commit()
    connection.close()


def _cur(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def _apply_migration(conn, filename):
    sql = (MIGRATIONS_DIR / filename).read_text()
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(sql)
    conn.commit()


def _seed_schema(conn):
    """Minimal pre-0015 shape: applications/loans/balances/payments, no
    accept_token column, no loans.app_id uniqueness -- the exact shape 0015
    is meant to repair."""
    with conn.cursor() as cur:
        cur.execute(f"""
            SET search_path TO {SCHEMA};
            CREATE TABLE applications (id SERIAL PRIMARY KEY);
            CREATE TABLE loans (
                id SERIAL PRIMARY KEY,
                app_id INTEGER,
                applicant_name TEXT,
                principal NUMERIC(14,2) NOT NULL,
                apr NUMERIC(7,3) NOT NULL,
                term_months INTEGER NOT NULL,
                status TEXT DEFAULT 'current'
            );
            CREATE TABLE balances (
                loan_id INTEGER PRIMARY KEY REFERENCES loans(id),
                balance NUMERIC(14,2) NOT NULL
            );
            CREATE TABLE payments (
                id SERIAL PRIMARY KEY,
                loan_id INTEGER REFERENCES loans(id),
                amount NUMERIC(14,2) NOT NULL
            );
        """)
    conn.commit()


def test_0015_migration_succeeds_on_duplicate_loans_with_no_payments(conn):
    """The common case: the race's loser was never returned to any caller, so
    nothing ever applied a payment against it. Must not raise, must keep
    exactly one loan per app_id, and must not leave an orphaned balances row
    behind for the loser."""
    _seed_schema(conn)
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("INSERT INTO applications (id) VALUES (1)")
        # Two loans boarded for the same application -- the exact race.
        cur.execute(
            "INSERT INTO loans (app_id, applicant_name, principal, apr, term_months) "
            "VALUES (1, 'Jane Borrower', 9000, 7.99, 24), "
            "(1, 'Jane Borrower', 9000, 7.99, 24) RETURNING id"
        )
        loan_ids = [r[0] for r in cur.fetchall()]
        for lid in loan_ids:
            cur.execute("INSERT INTO balances (loan_id, balance) VALUES (%s, 9000)", (lid,))
    conn.commit()

    # This must not raise -- a pre-fix 0015 aborts here with a unique
    # violation on loans_app_id_key.
    _apply_migration(conn, "0015_accept_token_and_loans_app_id_unique.sql")

    with _cur(conn) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT id FROM loans WHERE app_id = 1")
        surviving_loans = cur.fetchall()
        cur.execute("SELECT loan_id FROM balances")
        surviving_balances = [r["loan_id"] for r in cur.fetchall()]

    assert len(surviving_loans) == 1, "exactly one loan must survive per app_id"
    survivor_id = surviving_loans[0]["id"]
    assert surviving_balances == [survivor_id], "the loser's balances row must be dropped, not orphaned"

    with _cur(conn) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT conname FROM pg_constraint WHERE conname = 'loans_app_id_key'")
        assert cur.fetchall(), "loans_app_id_key must exist after 0015"


def test_0015_migration_keeps_the_loan_with_real_payment_history(conn):
    """If a payment was actually applied against one of the duplicates, that
    loan is real, external evidence of which one the borrower/app actually
    used -- it must survive regardless of id, and the payment must follow it
    (never be dropped)."""
    _seed_schema(conn)
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("INSERT INTO applications (id) VALUES (1)")
        cur.execute(
            "INSERT INTO loans (app_id, applicant_name, principal, apr, term_months) "
            "VALUES (1, 'Jane Borrower', 9000, 7.99, 24), "
            "(1, 'Jane Borrower', 9000, 7.99, 24) RETURNING id"
        )
        loan_ids = [r[0] for r in cur.fetchall()]
        for lid in loan_ids:
            cur.execute("INSERT INTO balances (loan_id, balance) VALUES (%s, 9000)", (lid,))
        # A real payment landed against the SECOND (higher-id) loan, not the
        # first -- the fix must keep that one even though it's not the oldest.
        paid_loan_id = loan_ids[1]
        cur.execute("INSERT INTO payments (loan_id, amount) VALUES (%s, 250)", (paid_loan_id,))
    conn.commit()

    _apply_migration(conn, "0015_accept_token_and_loans_app_id_unique.sql")

    with _cur(conn) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT id FROM loans WHERE app_id = 1")
        surviving = cur.fetchall()
        cur.execute("SELECT loan_id, amount FROM payments")
        payments = cur.fetchall()

    assert len(surviving) == 1
    assert surviving[0]["id"] == paid_loan_id, "the loan with real payment history must survive"
    assert len(payments) == 1, "the payment must never be dropped"
    assert payments[0]["loan_id"] == paid_loan_id
    assert float(payments[0]["amount"]) == pytest.approx(250)


def test_0015_migration_is_a_noop_on_data_with_no_duplicates(conn):
    """A database that never hit the race must be unaffected -- one loan per
    app_id survives untouched, and NULL app_id (the legacy direct-board path)
    stays legal for any number of rows."""
    _seed_schema(conn)
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("INSERT INTO applications (id) VALUES (1), (2)")
        cur.execute(
            "INSERT INTO loans (app_id, applicant_name, principal, apr, term_months) "
            "VALUES (1, 'Jane Borrower', 9000, 7.99, 24), "
            "(2, 'John Borrower', 5000, 6.5, 36), "
            "(NULL, 'Legacy Borrower', 1000, 5.0, 12), "
            "(NULL, 'Legacy Borrower Two', 2000, 5.0, 12) RETURNING id"
        )
        loan_ids = [r[0] for r in cur.fetchall()]
        for lid in loan_ids:
            cur.execute("INSERT INTO balances (loan_id, balance) VALUES (%s, 1000)", (lid,))
    conn.commit()

    _apply_migration(conn, "0015_accept_token_and_loans_app_id_unique.sql")

    with _cur(conn) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT id FROM loans ORDER BY id")
        rows = cur.fetchall()

    assert [r["id"] for r in rows] == loan_ids, "no rows should be touched when nothing is a duplicate"
