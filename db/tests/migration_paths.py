"""The repository's real schema-construction paths, in one place.

Both `test_migration_paths_converge.py` and
`test_no_card_data_on_either_schema_path.py` need to build a fresh database and
a genuinely migrated one. They used to do it separately, and the second grew a
hand-written `payments` table plus a `try/except psycopg2.Error` around the
migration chain -- so unrelated-but-required objects, constraints and ordering
interactions could fail, roll back, and never fail the test. A card-data proof
running against a schema shape production never has is not evidence, and a
reviewer was right to say so.

There is now one implementation. A test asserting a property of "the migrated
database" and the test asserting the migrations converge are talking about the
same database, because they call the same function.

Nothing here suppresses an error. A migration that fails, fails the test.
"""
import pathlib

import psycopg2
import psycopg2.extras

REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent.parent
INIT_DIR = REPO_ROOT / "db" / "init"
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"

# Schema-only init files. 002/003 are seed DATA -- irrelevant to a schema
# comparison, and 003 depends on rows 002 seeds.
#
# **007 was missing from this list, and that was the defect.** It reads as a
# back-fill -- it opens the ledger for seeded loans -- but it also DEFINES
# `capture_legacy_balance_delta` and `balances_cannot_be_deleted_during_cutover`
# and attaches all three ledger controls on `balances`. Leaving it out meant the
# "fresh" schema these comparisons were built on had the ledger tables and the
# projection but none of its controls: a database no operator has ever run,
# because the real init path applies every file in this directory in order.
#
# The comparison was therefore validating a fiction. Every aspect matched,
# because the migrated path was being compared against a fresh path that had
# been quietly hollowed out.
#
# With no seeds present 007's back-fill is a no-op, so including it costs
# nothing and makes "fresh" mean what it says.
INIT_SCHEMA_FILES = (
    "001_schema.sql", "004_decision_events.sql",
    "005_manual_reviews.sql", "006_decision_attempts.sql",
    "007_ledger_opening_balances.sql",
)


def _all_migrations():
    return sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name)


def _run_sql(conn, schema, sql):
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {schema}")
        # 0031 refuses to run without this acknowledgement -- it destroys data
        # and can break servicing instances still reading payments.pan. A test
        # harness IS the operator here, so it acknowledges explicitly rather
        # than the gate being weakened to let automation through. Set on every
        # statement because a GUC set with SET is session-scoped and these
        # helpers do not assume one long-lived session.
        cur.execute("SET meridian.pan_drop_acknowledged = 'yes'")
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
