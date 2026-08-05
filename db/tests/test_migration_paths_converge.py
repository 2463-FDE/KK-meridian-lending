"""PR #6 review, Gap D -- all four schema-construction paths must succeed and
converge.

The migrations could not be replayed on a database built from db/init. Four of
them added a UNIQUE constraint with a bare `ADD CONSTRAINT <name>`, where
<name> was exactly the auto-generated name Postgres had already assigned to the
same uniqueness declared INLINE in db/init:

    db/init/001_schema.sql  offers.app_id      UNIQUE -> offers_app_id_key
    db/init/001_schema.sql  offers.decision_id UNIQUE -> offers_decision_id_key
    db/init/001_schema.sql  loans.app_id       UNIQUE -> loans_app_id_key
    db/init/005_...    manual_reviews.app_id   UNIQUE -> manual_reviews_app_id_key

so `ADD CONSTRAINT` aborted with "already exists". CI documented this and
deliberately skipped the replay (.github/workflows/ci.yml's e2e job comment),
which meant NO job exercised the upgrade path end to end. Each of the four is
now guarded by a check on the COLUMN's uniqueness rather than on the
auto-generated name, so the guard holds even if the name ever differs.

The four paths:
  1. fresh init only                    -- what a new docker volume gets
  2. legacy pre-migration schema + all migrations -- a real upgrade
  3. fresh init THEN all migrations     -- the replay CI could not run
  4. migrations applied TWICE           -- idempotency

Paths 1, 3 and 4 must agree on the columns, constraints and indexes that
matter. Path 2 starts from a hand-written legacy shape and is asserted on the
specific objects the migrations are responsible for creating.
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

# Schema-only init files. 002/003 are seed DATA -- irrelevant to a schema
# comparison, and 003 depends on rows 002 seeds.
INIT_SCHEMA_FILES = (
    "001_schema.sql", "004_decision_events.sql",
    "005_manual_reviews.sql", "006_decision_attempts.sql",
)

SCHEMAS = {
    "fresh": "gapd_fresh_init",
    "legacy": "gapd_legacy_then_migrations",
    "replay": "gapd_fresh_then_migrations",
    "twice": "gapd_migrations_twice",
}


def _all_migrations():
    return sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name)


@pytest.fixture
def conn():
    connection = psycopg2.connect(DATABASE_URL)
    connection.autocommit = False
    with connection.cursor() as cur:
        for schema in SCHEMAS.values():
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            cur.execute(f"CREATE SCHEMA {schema}")
    connection.commit()
    yield connection
    with connection.cursor() as cur:
        for schema in SCHEMAS.values():
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    connection.commit()
    connection.close()


def _run_sql(conn, schema, sql):
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {schema}")
        cur.execute(sql)
    conn.commit()


def _build_fresh_init(conn, schema):
    for name in INIT_SCHEMA_FILES:
        _run_sql(conn, schema, (INIT_DIR / name).read_text())


def _has_executable_sql(sql: str) -> bool:
    """0001_initial.sql is three comment lines and no DDL (the migration chain
    has never had a reproducible baseline -- db/init is the baseline). psql
    treats such a file as a no-op; psycopg2's execute() raises on an empty
    query, so skip it explicitly rather than let a documentation-only file
    fail the run."""
    stripped = "\n".join(
        line for line in sql.splitlines() if line.strip() and not line.strip().startswith("--")
    )
    return bool(stripped.strip())


def _apply_all_migrations(conn, schema):
    """Every migration, in filename order -- the real upgrade sequence."""
    for path in _all_migrations():
        sql = path.read_text()
        if not _has_executable_sql(sql):
            continue
        _run_sql(conn, schema, sql)


def _build_legacy_schema(conn, schema):
    """A representative PRE-migration database: the shape that existed before
    0008, with the uniqueness the migrations are responsible for adding still
    ABSENT, plus real history rows that must survive."""
    _run_sql(conn, schema, """
        CREATE TABLE users (id SERIAL PRIMARY KEY, username TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'csr',
            display_name TEXT, applicant_id INTEGER, is_active BOOLEAN DEFAULT TRUE,
            created_at TIMESTAMPTZ DEFAULT now());
        CREATE TABLE applicants (id SERIAL PRIMARY KEY, name TEXT NOT NULL, dob DATE,
            ssn TEXT, ein TEXT, is_entity BOOLEAN DEFAULT FALSE, email TEXT,
            phone TEXT, address TEXT, created_at TIMESTAMPTZ DEFAULT now());
        CREATE TABLE applications (id SERIAL PRIMARY KEY,
            applicant_id INTEGER REFERENCES applicants(id),
            amount NUMERIC(14,2) NOT NULL, term_months INTEGER NOT NULL, purpose TEXT,
            income NUMERIC(14,2), employer TEXT, job_title TEXT,
            employment_years DOUBLE PRECISION, status TEXT DEFAULT 'submitted',
            created_at TIMESTAMPTZ DEFAULT now());
        CREATE TABLE kyc_checks (id SERIAL PRIMARY KEY,
            applicant_id INTEGER REFERENCES applicants(id), name_verified BOOLEAN,
            dob_verified BOOLEAN, address_verified BOOLEAN, ssn_verified BOOLEAN,
            created_at TIMESTAMPTZ DEFAULT now());
        CREATE TABLE decisions (app_id INTEGER PRIMARY KEY REFERENCES applications(id),
            outcome TEXT NOT NULL);
        -- no UNIQUE on app_id/decision_id yet: 0009/0011 add them
        CREATE TABLE offers (id SERIAL PRIMARY KEY,
            app_id INTEGER REFERENCES applications(id),
            apr NUMERIC(7,3), finance_charge NUMERIC(14,2), monthly_payment NUMERIC(14,2),
            amount_financed NUMERIC(14,2), total_of_payments NUMERIC(14,2),
            created_at TIMESTAMPTZ DEFAULT now());
        -- no UNIQUE on app_id yet: 0015 adds it
        CREATE TABLE loans (id SERIAL PRIMARY KEY, app_id INTEGER, applicant_name TEXT,
            principal NUMERIC(14,2) NOT NULL, apr NUMERIC(7,3) NOT NULL,
            term_months INTEGER NOT NULL, status TEXT DEFAULT 'current',
            opened_at TIMESTAMPTZ DEFAULT now());
        CREATE TABLE balances (loan_id INTEGER PRIMARY KEY REFERENCES loans(id),
            balance NUMERIC(14,2) NOT NULL, past_due NUMERIC(14,2) DEFAULT 0,
            updated_at TIMESTAMPTZ DEFAULT now());
        CREATE TABLE payments (id SERIAL PRIMARY KEY,
            loan_id INTEGER REFERENCES loans(id), pan TEXT,
            amount NUMERIC(14,2) NOT NULL, method TEXT DEFAULT 'card',
            created_at TIMESTAMPTZ DEFAULT now());
        CREATE TABLE audit_logs (id SERIAL PRIMARY KEY, actor TEXT, action TEXT,
            detail TEXT, deleted_at TIMESTAMPTZ, created_at TIMESTAMPTZ DEFAULT now());

        INSERT INTO applicants (id, name) VALUES (1, 'Sam Okafor');
        INSERT INTO applications (id, applicant_id, amount, term_months)
            VALUES (1, 1, 9000, 24);
        INSERT INTO decisions (app_id, outcome) VALUES (1, 'refer');
        INSERT INTO offers (app_id, apr, finance_charge, monthly_payment,
                            amount_financed, total_of_payments)
            VALUES (1, 5.946, 768.11, 407.0, 8730.0, 9768.11);
        INSERT INTO loans (id, app_id, applicant_name, principal, apr, term_months)
            VALUES (1, 1, 'Sam Okafor', 9000, 5.946, 24);
    """)


# --- helpers to compare shapes ------------------------------------------------

def _columns(conn, schema, table):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY column_name",
            (schema, table),
        )
        return {r["column_name"]: (r["data_type"], r["is_nullable"]) for r in cur.fetchall()}


def _unique_columns(conn, schema, table):
    """Every column that carries a single-column UNIQUE constraint, by NAME of
    the column -- deliberately not by constraint name, since the whole bug was
    a name collision."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT a.attname FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = ANY(c.conkey) "
            "WHERE n.nspname = %s AND t.relname = %s AND c.contype = 'u' "
            "AND array_length(c.conkey, 1) = 1",
            (schema, table),
        )
        return {r[0] for r in cur.fetchall()}


# --- the four paths -----------------------------------------------------------

def test_path_1_fresh_init_only_succeeds(conn):
    _build_fresh_init(conn, SCHEMAS["fresh"])
    assert "access_token_hash" in _columns(conn, SCHEMAS["fresh"], "applications")


def test_path_2_legacy_schema_plus_all_migrations_succeeds(conn):
    """A real upgrade: every migration, in order, over a pre-migration shape."""
    _build_legacy_schema(conn, SCHEMAS["legacy"])
    _apply_all_migrations(conn, SCHEMAS["legacy"])

    schema = SCHEMAS["legacy"]
    # The migrations are responsible for these.
    assert "app_id" in _unique_columns(conn, schema, "offers")
    assert "decision_id" in _unique_columns(conn, schema, "offers")
    assert "app_id" in _unique_columns(conn, schema, "loans")
    assert "app_id" in _unique_columns(conn, schema, "manual_reviews")
    cols = _columns(conn, schema, "applications")
    assert "access_token" not in cols, "0025 must drop the plaintext column"
    assert "access_token_hash" in cols and "accept_token_hash" in cols
    assert "attempt_id" in _columns(conn, schema, "decision_events")


def test_path_2_preserves_existing_history(conn):
    """"Do not delete or overwrite valid review history" -- the dedupe steps in
    0011/0015/0020 must be no-ops on a database that has no duplicates."""
    schema = SCHEMAS["legacy"]
    _build_legacy_schema(conn, schema)
    _apply_all_migrations(conn, schema)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {schema}")
        cur.execute("SELECT outcome FROM decisions WHERE app_id = 1")
        assert cur.fetchone()["outcome"] == "refer"
        cur.execute("SELECT apr FROM offers WHERE app_id = 1")
        assert float(cur.fetchone()["apr"]) == 5.946
        cur.execute("SELECT applicant_name FROM loans WHERE app_id = 1")
        assert cur.fetchone()["applicant_name"] == "Sam Okafor"


def test_path_3_fresh_init_then_migration_runner_succeeds(conn):
    """The path CI explicitly could not run before this fix."""
    schema = SCHEMAS["replay"]
    _build_fresh_init(conn, schema)
    _apply_all_migrations(conn, schema)   # must not raise

    assert "app_id" in _unique_columns(conn, schema, "manual_reviews")
    assert "access_token" not in _columns(conn, schema, "applications")


def test_path_4_migrations_are_idempotent_when_replayed(conn):
    schema = SCHEMAS["twice"]
    _build_fresh_init(conn, schema)
    _apply_all_migrations(conn, schema)
    _apply_all_migrations(conn, schema)   # second pass must also not raise

    assert "app_id" in _unique_columns(conn, schema, "manual_reviews")
    assert "access_token" not in _columns(conn, schema, "applications")


# --- convergence ---------------------------------------------------------------

_CONVERGENCE_TABLES = (
    "applications", "offers", "loans", "manual_reviews",
    "decision_events", "decision_attempts", "payments", "payment_applications",
)


def test_all_three_init_based_paths_converge(conn):
    """Fresh init, fresh init + migrations, and fresh init + migrations twice
    must produce the same columns and the same single-column UNIQUE sets on
    every table this PR touches."""
    _build_fresh_init(conn, SCHEMAS["fresh"])

    _build_fresh_init(conn, SCHEMAS["replay"])
    _apply_all_migrations(conn, SCHEMAS["replay"])

    _build_fresh_init(conn, SCHEMAS["twice"])
    _apply_all_migrations(conn, SCHEMAS["twice"])
    _apply_all_migrations(conn, SCHEMAS["twice"])

    for table in _CONVERGENCE_TABLES:
        fresh_cols = _columns(conn, SCHEMAS["fresh"], table)
        assert fresh_cols, f"{table} missing from the fresh-init schema"
        for other in ("replay", "twice"):
            assert _columns(conn, SCHEMAS[other], table) == fresh_cols, (
                f"{table} columns diverge between fresh-init and {other}"
            )
            assert _unique_columns(conn, SCHEMAS[other], table) == \
                   _unique_columns(conn, SCHEMAS["fresh"], table), (
                f"{table} UNIQUE columns diverge between fresh-init and {other}"
            )


def test_the_four_previously_colliding_constraints_are_guarded(conn):
    """Regression guard naming the exact four. Applying each of them to a
    fresh-init schema (which already has the inline UNIQUE) must be a no-op,
    not an 'already exists' abort."""
    schema = SCHEMAS["fresh"]
    _build_fresh_init(conn, schema)
    # Applied as the ordered whole, which is how a runner reaches them --
    # 0011/0015/0020 depend on objects earlier migrations create.
    _apply_all_migrations(conn, schema)
    for table, column in (("offers", "decision_id"), ("offers", "app_id"),
                          ("loans", "app_id"), ("manual_reviews", "app_id")):
        assert column in _unique_columns(conn, schema, table), (
            f"{table}.{column} lost its uniqueness across the replay"
        )
