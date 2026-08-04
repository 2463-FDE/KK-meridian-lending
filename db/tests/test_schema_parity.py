"""Schema-parity proof: a fresh Docker volume (db/init/*.sql, auto-applied
by Postgres on first boot) and an EXISTING database upgraded through
migration 0022 must end up with the identical accept_token schema shape.

Audit finding: db/init/001_schema.sql still created the old plaintext
`accept_token TEXT` column, never updated to match migration 0022's hash/
expiry/consumed design -- a brand new environment recreated the exact
vulnerability 0022 exists to close, until someone happened to apply 0022
by hand afterward. Fixed by backporting 0022's shape directly into
db/init/001_schema.sql (see that file's own comment). This test is the
automated guard against that drift recurring silently -- it fails loudly
in CI if the two paths ever diverge again, instead of relying on someone
noticing by inspection.

Two independent throwaway schemas, both real Postgres (DATABASE_URL):
  - fresh_init: db/init/001_schema.sql + 004_decision_events.sql +
    005_manual_reviews.sql applied verbatim, exactly what a brand new
    docker-compose volume ends up with (002/003 are pure seed data, not
    schema, and are skipped here on purpose).
  - migrated: a representative PRE-0022 shape (old plaintext
    accept_token TEXT, plus a decisions/manual_reviews row standing in for
    real history) with migration 0022 applied verbatim from disk on top,
    exactly what upgrading an existing, already-deployed database does.
"""
import os
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
INIT_DIR = REPO_ROOT / "db" / "init"
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"

FRESH_SCHEMA = "schema_parity_fresh_init"
MIGRATED_SCHEMA = "schema_parity_migrated"


@pytest.fixture
def conn():
    connection = psycopg2.connect(DATABASE_URL)
    connection.autocommit = False
    with connection.cursor() as cur:
        for schema in (FRESH_SCHEMA, MIGRATED_SCHEMA):
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            cur.execute(f"CREATE SCHEMA {schema}")
    connection.commit()
    yield connection
    with connection.cursor() as cur:
        for schema in (FRESH_SCHEMA, MIGRATED_SCHEMA):
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    connection.commit()
    connection.close()


def _run_sql_file(conn, schema, path):
    sql = path.read_text()
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {schema}")
        cur.execute(sql)
    conn.commit()


def _build_fresh_init(conn):
    """Exactly what docker-compose's fresh Postgres volume runs, schema
    files only (002/003 are seed data -- irrelevant to a schema comparison,
    and 003 depends on rows 002 seeds that this test has no need for)."""
    for filename in ("001_schema.sql", "004_decision_events.sql", "005_manual_reviews.sql"):
        _run_sql_file(conn, FRESH_SCHEMA, INIT_DIR / filename)


def _build_pre_0022_schema_with_history(conn):
    """A representative shape of an existing, already-deployed database
    the moment before migration 0022 runs: the OLD plaintext accept_token
    column (as 0015 originally added it), plus real decision/manual-review
    history that must survive the upgrade untouched."""
    with conn.cursor() as cur:
        cur.execute(f"""
            SET search_path TO {MIGRATED_SCHEMA};
            CREATE TABLE applications (
                id SERIAL PRIMARY KEY,
                status TEXT DEFAULT 'submitted',
                accept_token TEXT,
                created_at TIMESTAMPTZ DEFAULT now()
            );
            CREATE TABLE decisions (
                app_id INTEGER PRIMARY KEY REFERENCES applications(id),
                outcome TEXT NOT NULL
            );
            CREATE TABLE manual_reviews (
                id SERIAL PRIMARY KEY,
                app_id INTEGER NOT NULL REFERENCES applications(id) UNIQUE,
                reviewer_role TEXT NOT NULL,
                reviewer_name TEXT,
                outcome TEXT NOT NULL,
                reason TEXT NOT NULL,
                reviewed_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            INSERT INTO applications (id, status, accept_token)
                VALUES (1, 'approved', 'a-real-looking-plaintext-token-value');
            INSERT INTO decisions (app_id, outcome) VALUES (1, 'approve');
            INSERT INTO manual_reviews (app_id, reviewer_role, reviewer_name, outcome, reason)
                VALUES (1, 'underwriter', 'Sam Okafor', 'approve', 'DTI recalculated under policy');
        """)
    conn.commit()


def _columns(conn, schema, table):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s "
            "ORDER BY column_name",
            (schema, table),
        )
        return {r["column_name"]: (r["data_type"], r["is_nullable"]) for r in cur.fetchall()}


def _indexes(conn, schema, table):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT indexname FROM pg_indexes WHERE schemaname = %s AND tablename = %s",
            (schema, table),
        )
        return {r[0] for r in cur.fetchall()}


def test_fresh_init_has_no_plaintext_accept_token_column(conn):
    _build_fresh_init(conn)
    cols = _columns(conn, FRESH_SCHEMA, "applications")
    assert "accept_token" not in cols
    assert "accept_token_hash" in cols
    assert "accept_token_expires_at" in cols
    assert "accept_token_consumed_at" in cols


def test_migration_0022_removes_the_plaintext_column_and_preserves_history(conn):
    _build_pre_0022_schema_with_history(conn)
    cols_before = _columns(conn, MIGRATED_SCHEMA, "applications")
    assert "accept_token" in cols_before  # sanity: the vulnerable shape really existed

    _run_sql_file(conn, MIGRATED_SCHEMA, MIGRATIONS_DIR / "0022_accept_token_hash.sql")

    cols_after = _columns(conn, MIGRATED_SCHEMA, "applications")
    assert "accept_token" not in cols_after, "no raw token column may survive the upgrade"
    assert "accept_token_hash" in cols_after
    assert "accept_token_expires_at" in cols_after
    assert "accept_token_consumed_at" in cols_after

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {MIGRATED_SCHEMA}")
        cur.execute("SELECT outcome FROM decisions WHERE app_id = 1")
        assert cur.fetchone()["outcome"] == "approve"
        cur.execute("SELECT reviewer_name, outcome, reason FROM manual_reviews WHERE app_id = 1")
        row = cur.fetchone()
        assert row["reviewer_name"] == "Sam Okafor"
        assert row["outcome"] == "approve"
        assert row["reason"] == "DTI recalculated under policy"


def test_fresh_init_and_migrated_schemas_agree_on_the_accept_token_columns(conn):
    """The actual parity check CI needs: build BOTH paths independently,
    then diff the exact column set/types/nullability for the fields this
    security fix touches. A future edit to only one side (0022 changed
    again but db/init left stale, or vice versa) fails this test loudly."""
    _build_fresh_init(conn)
    _build_pre_0022_schema_with_history(conn)
    _run_sql_file(conn, MIGRATED_SCHEMA, MIGRATIONS_DIR / "0022_accept_token_hash.sql")

    fresh_cols = _columns(conn, FRESH_SCHEMA, "applications")
    migrated_cols = _columns(conn, MIGRATED_SCHEMA, "applications")

    token_fields = ["accept_token_hash", "accept_token_expires_at", "accept_token_consumed_at"]
    for field in token_fields:
        assert field in fresh_cols, f"{field} missing from fresh-init schema"
        assert field in migrated_cols, f"{field} missing from migrated schema"
        assert fresh_cols[field] == migrated_cols[field], (
            f"{field} differs between fresh-init {fresh_cols[field]} and migrated {migrated_cols[field]}"
        )

    assert "accept_token" not in fresh_cols
    assert "accept_token" not in migrated_cols

    fresh_indexes = _indexes(conn, FRESH_SCHEMA, "applications")
    migrated_indexes = _indexes(conn, MIGRATED_SCHEMA, "applications")
    assert "idx_applications_accept_token_hash" in fresh_indexes
    assert "idx_applications_accept_token_hash" in migrated_indexes
