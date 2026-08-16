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
INIT_SCHEMA_FILES = (
    "001_schema.sql", "004_decision_events.sql",
    "005_manual_reviews.sql", "006_decision_attempts.sql",
)


def _all_migrations():
    return sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name)


def _run_sql(conn, schema, sql):
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {schema}")
        # 0031 and 0039 refuse to run without these acknowledgements -- both are
        # contract-half migrations that destroy data and can break instances
        # still reading the dropped column (`payments.pan`, `loans.apr`). A test
        # harness IS the operator here, so it acknowledges explicitly rather
        # than the gates being weakened to let automation through. Set on every
        # statement because a GUC set with SET is session-scoped and these
        # helpers do not assume one long-lived session.
        cur.execute("SET meridian.pan_drop_acknowledged = 'yes'")
        cur.execute("SET meridian.loans_apr_drop_acknowledged = 'yes'")
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


# Migrations that cannot run unattended, and the operator step each one
# requires first. Keyed by filename prefix, run immediately before that file.
#
# These are NOT fixes to the migrations and must never grow into a way of
# getting a red chain green. Each entry is a HUMAN decision that the migration
# deliberately refuses to make, transcribed for a synthetic database whose
# history this repository authored.
_OPERATOR_STEPS = {
    # `docs/RUNBOOK-loans-apr-contract.md` step 2. 0039 refuses while any loan
    # has an unproven `note_rate_pct`, and the legacy fixture has exactly one:
    # loan 1, whose offer's payment (407.00) does not reproduce from its rate
    # (5.946% over 24mo bills 398.68), so 0030 could not prove the offer and
    # 0038 could not back-fill from it. That refusal is correct and is asserted
    # directly in `db/tests/test_0039_drop_loans_apr.py`.
    #
    # A real operator resolves it from the signed disclosure or the servicing
    # history. Here the fixture IS the history -- `_build_legacy_schema` wrote
    # that loan at 5.946% and nothing else ever billed it -- so the answer is
    # known rather than guessed, and it is recorded here where a reader can see
    # it is a fixture premise and not a rule the migration applies.
    #
    # Nothing generalises from this. On a real database the same UPDATE would be
    # relabelling a possibly-disclosed APR as a contractual term, which is what
    # the gate exists to stop.
    # Guarded on the column's existence, because this same helper builds paths
    # that never had `apr` at all -- a fresh `db/init` database run through the
    # migration runner, and a replay of the whole chain after 0039 has already
    # dropped it. On those paths there is nothing to resolve and the step is a
    # no-op, which is different from the step being unnecessary.
    "0039": """
        DO $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM information_schema.columns
                 WHERE table_schema = current_schema()
                   AND table_name = 'loans' AND column_name = 'apr'
            ) THEN
                EXECUTE 'UPDATE loans SET note_rate_pct = 5.946 '
                        ' WHERE note_rate_pct IS NULL AND apr = 5.946';
            END IF;
        END $$;
    """,
}


def _apply_all_migrations(conn, schema):
    """Every migration, in filename order -- the real upgrade sequence."""
    for path in _all_migrations():
        sql = path.read_text()
        if not _has_executable_sql(sql):
            continue
        step = _OPERATOR_STEPS.get(path.name.split("_")[0])
        if step:
            _run_sql(conn, schema, step)
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
        -- `apr`, NOT `note_rate_pct`. This is the LEGACY shape -- the database as
        -- it was before the migration chain ran -- and `note_rate_pct` is what
        -- 0038 adds and 0039 makes NOT NULL. Naming the new column here would
        -- make the convergence tests assert against a starting point that never
        -- existed, and would silently skip the two migrations being tested.
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
