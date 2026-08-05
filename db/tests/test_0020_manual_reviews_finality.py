"""Integration test for db/migrations/0020_manual_reviews_finality.sql.

Review finding: manual_reviews had no constraint stopping more than one row
per app_id -- a since-reverted "override" design relied entirely on
application-layer checks to keep it to one, and this repo's own dev/test
data actually accumulated several rows for the same app_id under that
design. Adding `UNIQUE (app_id)` with no cleanup first would fail on exactly
that data, same shape of bug as 0011/0015 before their own fixes.

Runs against a real Postgres (DATABASE_URL) rather than mocking SQL, since
the bug is in migration-time data cleanup. Skips if DATABASE_URL isn't set;
the CI db-migrations job always sets it.
"""
import os
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
SCHEMA = "migration_test_0020"


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
    """Pre-0020 shape: applications/manual_reviews, no reviewer_name column,
    no app_id uniqueness -- the exact shape 0020 is meant to repair."""
    with conn.cursor() as cur:
        cur.execute(f"""
            SET search_path TO {SCHEMA};
            CREATE TABLE applications (id SERIAL PRIMARY KEY);
            CREATE TABLE manual_reviews (
                id SERIAL PRIMARY KEY,
                app_id INTEGER NOT NULL REFERENCES applications(id),
                reviewer_role TEXT NOT NULL,
                outcome TEXT NOT NULL,
                reason TEXT NOT NULL,
                reviewed_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
    conn.commit()


def test_0020_migration_succeeds_on_duplicate_manual_reviews(conn):
    """The exact dirty-data case: several manual_reviews rows already exist
    for one app_id (from the since-reverted override design). Must not
    raise, and must keep exactly the ORIGINAL (oldest) row -- the first
    decision is what must stay final."""
    _seed_schema(conn)
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("INSERT INTO applications (id) VALUES (1)")
        cur.execute(
            "INSERT INTO manual_reviews (app_id, reviewer_role, outcome, reason) VALUES "
            "(1, 'underwriter', 'deny', 'Low income'), "
            "(1, 'underwriter', 'deny', 'race-guard smoke test override'), "
            "(1, 'underwriter', 'approve', 'Aproved')"
        )
    conn.commit()

    # This must not raise -- a pre-fix 0020 aborts here with a unique
    # violation on manual_reviews_app_id_key.
    _apply_migration(conn, "0020_manual_reviews_finality.sql")

    with _cur(conn) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT outcome, reason FROM manual_reviews WHERE app_id = 1")
        rows = cur.fetchall()

    assert len(rows) == 1, "exactly one manual review must survive per app_id"
    assert rows[0]["outcome"] == "deny"
    assert rows[0]["reason"] == "Low income", "the FIRST (original) decision must survive, not the latest"

    with _cur(conn) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT conname FROM pg_constraint WHERE conname = 'manual_reviews_app_id_key'")
        assert cur.fetchall(), "manual_reviews_app_id_key must exist after 0020"
        cur.execute("SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'manual_reviews' AND column_name = 'reviewer_name'")
        assert cur.fetchall(), "reviewer_name column must exist after 0020"


def test_0020_migration_is_a_noop_on_data_with_no_duplicates(conn):
    _seed_schema(conn)
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("INSERT INTO applications (id) VALUES (1), (2)")
        cur.execute(
            "INSERT INTO manual_reviews (app_id, reviewer_role, outcome, reason) VALUES "
            "(1, 'underwriter', 'approve', 'DTI ok'), "
            "(2, 'csr', 'deny', 'Income too low') RETURNING id"
        )
        ids = [r[0] for r in cur.fetchall()]
    conn.commit()

    _apply_migration(conn, "0020_manual_reviews_finality.sql")

    with _cur(conn) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT id FROM manual_reviews ORDER BY id")
        rows = cur.fetchall()

    assert [r["id"] for r in rows] == ids
