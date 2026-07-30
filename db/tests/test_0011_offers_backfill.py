"""Integration test for db/migrations/0011_offers_backfill_and_app_id_unique.sql.

Review finding: 0011's original ordering backfilled decision_id (step 1)
BEFORE resolving duplicate legacy offers (step 3). Backfilling sets
decision_id = app_id, so two duplicate rows for the same app_id got the SAME
decision_id -- which immediately violated 0009's offers_decision_id_key
UNIQUE constraint and aborted the whole migration, on exactly the data this
migration exists to repair.

This runs against a real Postgres (needs DATABASE_URL — see docker-compose.yml
for the default local instance) rather than mocking SQL, since the bug is in
statement ORDERING and constraint interaction, not in application code. Skips
if DATABASE_URL isn't set (no DB reachable locally); the CI db-migrations job
always sets it, so this never silently skips there.
"""
import os
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "migrations"
SCHEMA = "migration_test_0011"


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


def _seed_legacy_schema_with_duplicate_offers(conn):
    """Builds the pre-0008 shape of applications/decisions/offers (no
    decision_id/fee_pct_used columns, no app_id uniqueness) and seeds two
    duplicate legacy offers for the same application -- the exact shape 0011
    is meant to repair. Every legacy offer predates decision_id entirely, so
    it's NULL on every row, same as a real pre-W4 database."""
    with conn.cursor() as cur:
        cur.execute(f"""
            SET search_path TO {SCHEMA};
            CREATE TABLE applications (id SERIAL PRIMARY KEY);
            CREATE TABLE decisions (
                app_id INTEGER PRIMARY KEY REFERENCES applications(id),
                outcome TEXT
            );
            CREATE TABLE offers (
                id SERIAL PRIMARY KEY,
                app_id INTEGER REFERENCES applications(id),
                apr NUMERIC(7,3),
                finance_charge NUMERIC(14,2),
                monthly_payment NUMERIC(14,2),
                amount_financed NUMERIC(14,2),
                total_of_payments NUMERIC(14,2),
                created_at TIMESTAMPTZ DEFAULT now()
            );
        """)
        cur.execute("INSERT INTO applications (id) VALUES (1) RETURNING id")
        cur.execute("INSERT INTO decisions (app_id, outcome) VALUES (1, 'approve')")
        # Two duplicate legacy offers for the same app_id -- both decision_id-less
        # (the column doesn't exist yet at this point in history).
        cur.execute(
            "INSERT INTO offers (app_id, apr) VALUES (1, 12.5), (1, 12.5) RETURNING id"
        )
    conn.commit()


def test_0011_migration_succeeds_on_duplicate_legacy_offers(conn):
    _seed_legacy_schema_with_duplicate_offers(conn)

    # Prerequisite migrations: 0008 adds decision_id/fee_pct_used, 0009 adds
    # the decision_id UNIQUE constraint that a pre-fix 0011 could no longer
    # satisfy once backfill ran before dedup.
    _apply_migration(conn, "0008_offer_decision_link.sql")
    _apply_migration(conn, "0009_offers_decision_id_unique.sql")

    # This must not raise -- a pre-fix 0011 aborts here with a unique
    # violation on offers_decision_id_key.
    _apply_migration(conn, "0011_offers_backfill_and_app_id_unique.sql")

    with _cur(conn) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT * FROM offers ORDER BY id")
        rows = cur.fetchall()

    assert len(rows) == 1, "the older duplicate offer must have been deleted"
    row = rows[0]
    assert row["decision_id"] == 1
    assert float(row["fee_pct_used"]) == pytest.approx(0.030)

    with _cur(conn) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT conname FROM pg_constraint WHERE conname = 'offers_app_id_key'")
        assert cur.fetchall(), "offers_app_id_key must exist after 0011"


def test_0011_migration_keeps_newest_offer_on_duplicate(conn):
    _seed_legacy_schema_with_duplicate_offers(conn)
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        # A third, distinguishable offer for the same app -- the newest one,
        # by id -- so we can assert 0011 kept the RIGHT row, not just one row.
        cur.execute("INSERT INTO offers (app_id, apr) VALUES (1, 99.9)")
    conn.commit()

    _apply_migration(conn, "0008_offer_decision_link.sql")
    _apply_migration(conn, "0009_offers_decision_id_unique.sql")
    _apply_migration(conn, "0011_offers_backfill_and_app_id_unique.sql")

    with _cur(conn) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT apr FROM offers")
        rows = cur.fetchall()

    assert len(rows) == 1
    assert float(rows[0]["apr"]) == pytest.approx(99.9)
