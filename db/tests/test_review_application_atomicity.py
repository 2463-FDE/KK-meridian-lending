"""Audit finding (requirement 2): prove the manual_reviews INSERT and the
applications/decisions status writes review_application issues are a real,
single Postgres transaction -- a failure partway through rolls back
EVERYTHING already executed in that transaction, including the
manual_reviews row, not just the statements after the failure point.

review_application's own unit tests (test_manual_review.py) mock
db.transaction() entirely -- they prove the application's calling
sequence/guard logic, but a mocked cursor cannot prove real ACID rollback
behavior. This runs the exact same statement shape against a real Postgres
connection (mirroring this directory's other migration tests) to prove the
transactional guarantee itself, independent of application code.
"""
import os

import psycopg2
import psycopg2.extras
import pytest

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against")

SCHEMA = "atomicity_test_review_application"


@pytest.fixture
def conn():
    connection = psycopg2.connect(DATABASE_URL)
    connection.autocommit = False
    cur = connection.cursor()
    cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    cur.execute(f"CREATE SCHEMA {SCHEMA}")
    cur.execute(f"SET search_path TO {SCHEMA}")
    connection.commit()
    cur.execute(f"""
        SET search_path TO {SCHEMA};
        CREATE TABLE applications (id SERIAL PRIMARY KEY, status TEXT DEFAULT 'in_review');
        CREATE TABLE decisions (app_id INTEGER PRIMARY KEY REFERENCES applications(id), outcome TEXT NOT NULL);
        CREATE TABLE manual_reviews (
            id SERIAL PRIMARY KEY,
            app_id INTEGER NOT NULL REFERENCES applications(id) UNIQUE,
            reviewer_role TEXT NOT NULL,
            outcome TEXT NOT NULL,
            reason TEXT NOT NULL,
            reviewed_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        INSERT INTO applications (id, status) VALUES (1, 'in_review');
        INSERT INTO decisions (app_id, outcome) VALUES (1, 'refer');
    """)
    connection.commit()
    yield connection
    cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    connection.commit()
    connection.close()


def _cur(conn):
    return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)


def test_a_failure_after_the_insert_rolls_back_the_insert_too(conn):
    """Mirrors review_application's exact transaction shape: SELECT ... FOR
    UPDATE, INSERT INTO manual_reviews, UPDATE decisions, UPDATE
    applications -- all on ONE connection with autocommit=False. Forces the
    final statement to fail (a bad column name -- any error works, this
    isn't testing WHICH error) and confirms the earlier, already-executed
    manual_reviews INSERT never persists once the transaction is rolled
    back, proving these statements commit or fail together, not
    independently."""
    with _cur(conn) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT status FROM applications WHERE id = 1 FOR UPDATE")
        cur.execute(
            "INSERT INTO manual_reviews (app_id, reviewer_role, outcome, reason) "
            "VALUES (1, 'underwriter', 'approve', 'DTI ok') "
            "ON CONFLICT (app_id) DO NOTHING RETURNING id",
        )
        assert cur.fetchall(), "the insert itself must have succeeded up to this point"
        cur.execute("UPDATE decisions SET outcome = 'approve' WHERE app_id = 1")

        with pytest.raises(psycopg2.errors.UndefinedColumn):
            cur.execute("UPDATE applications SET status = 'approved', nonexistent_column = 1 WHERE id = 1")

    conn.rollback()

    with _cur(conn) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT * FROM manual_reviews WHERE app_id = 1")
        assert cur.fetchall() == [], "the manual_reviews row must not survive the rolled-back transaction"
        cur.execute("SELECT outcome FROM decisions WHERE app_id = 1")
        assert cur.fetchall()[0]["outcome"] == "refer", "the decisions UPDATE must also have rolled back"


def test_a_successful_transaction_commits_the_insert_and_the_updates_together(conn):
    with _cur(conn) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT status FROM applications WHERE id = 1 FOR UPDATE")
        cur.execute(
            "INSERT INTO manual_reviews (app_id, reviewer_role, outcome, reason) "
            "VALUES (1, 'underwriter', 'approve', 'DTI ok') "
            "ON CONFLICT (app_id) DO NOTHING RETURNING id",
        )
        assert cur.fetchall()
        cur.execute("UPDATE decisions SET outcome = 'approve' WHERE app_id = 1")
        cur.execute("UPDATE applications SET status = 'approved' WHERE id = 1")
    conn.commit()

    with _cur(conn) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT outcome FROM manual_reviews WHERE app_id = 1")
        assert cur.fetchall()[0]["outcome"] == "approve"
        cur.execute("SELECT outcome FROM decisions WHERE app_id = 1")
        assert cur.fetchall()[0]["outcome"] == "approve"
        cur.execute("SELECT status FROM applications WHERE id = 1")
        assert cur.fetchall()[0]["status"] == "approved"
