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
    for filename in (
        "001_schema.sql", "004_decision_events.sql", "005_manual_reviews.sql",
        "006_decision_attempts.sql", "007_ledger_opening_balances.sql",
    ):
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


def _build_pre_0023_schema_with_history(conn):
    """A representative shape of an existing, already-deployed database the
    moment before migration 0023 runs: applications/decisions/manual_reviews
    (as above) plus a real, pre-existing decision_events table (004's actual
    shape -- no attempt_id column yet) with one real history row that must
    survive the upgrade untouched."""
    with conn.cursor() as cur:
        cur.execute(f"""
            SET search_path TO {MIGRATED_SCHEMA};
            CREATE TABLE applications (
                id SERIAL PRIMARY KEY,
                status TEXT DEFAULT 'submitted',
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
            CREATE TABLE decision_events (
                id                SERIAL PRIMARY KEY,
                app_id            INTEGER NOT NULL REFERENCES applications(id),
                occurred_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
                requested_amount  NUMERIC(14,2),
                term_months       INTEGER,
                annual_income     NUMERIC(14,2),
                bureau_score      INTEGER,
                model_score       DOUBLE PRECISION,
                model_version     TEXT NOT NULL,
                top_features      JSONB,
                decision          TEXT NOT NULL,
                reason_codes      JSONB NOT NULL DEFAULT '[]'::jsonb
            );
            INSERT INTO applications (id, status) VALUES (1, 'approved');
            INSERT INTO decisions (app_id, outcome) VALUES (1, 'approve');
            INSERT INTO decision_events
                (app_id, requested_amount, term_months, annual_income, bureau_score,
                 model_score, model_version, top_features, decision, reason_codes)
                VALUES (1, 9000, 24, 40000, 710, 0.82, 'v1', '["bureau_score"]'::jsonb,
                        'approve', '[]'::jsonb);
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


def test_fresh_init_has_decision_attempts_with_constraints(conn):
    _build_fresh_init(conn)
    cols = _columns(conn, FRESH_SCHEMA, "decision_attempts")
    for field in ("id", "app_id", "state", "requested_by", "started_at",
                  "lease_expires_at", "completed_at", "failure_code", "failure_detail"):
        assert field in cols, f"{field} missing from fresh-init decision_attempts"

    events_cols = _columns(conn, FRESH_SCHEMA, "decision_events")
    assert "attempt_id" in events_cols

    indexes = _indexes(conn, FRESH_SCHEMA, "decision_attempts")
    assert "idx_decision_attempts_one_active" in indexes
    event_indexes = _indexes(conn, FRESH_SCHEMA, "decision_events")
    assert "idx_decision_events_attempt_id" in event_indexes


def test_fresh_init_decision_attempts_constraints_reject_invalid_rows(conn):
    """The CHECK constraints, not just the columns, must exist on a fresh
    volume -- proven by attempting inserts that violate each one."""
    _build_fresh_init(conn)
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {FRESH_SCHEMA}")
        cur.execute("INSERT INTO applications (id, amount, term_months, status) VALUES (1, 9000, 24, 'submitted')")
    conn.commit()

    def _rejects(sql):
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {FRESH_SCHEMA}")
            with pytest.raises(psycopg2.errors.CheckViolation):
                cur.execute(sql)
        conn.rollback()

    _rejects("INSERT INTO decision_attempts (app_id, state) VALUES (1, 'bogus_state')")
    _rejects(
        "INSERT INTO decision_attempts (app_id, state, lease_expires_at) "
        "VALUES (1, 'in_progress', NULL)"
    )
    _rejects(
        "INSERT INTO decision_attempts (app_id, state, completed_at) "
        "VALUES (1, 'completed', NULL)"
    )
    _rejects(
        "INSERT INTO decision_attempts (app_id, state, completed_at, failure_code) "
        "VALUES (1, 'completed', now(), 'timeout')"
    )
    _rejects(
        "INSERT INTO decision_attempts (app_id, state, completed_at) "
        "VALUES (1, 'failed', now())"
    )
    _rejects(
        "INSERT INTO decision_attempts (app_id, state, completed_at, failure_code) "
        "VALUES (1, 'failed', now(), 'not_a_real_code')"
    )

    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {FRESH_SCHEMA}")
        cur.execute(
            "INSERT INTO decision_attempts (app_id, state, lease_expires_at) "
            "VALUES (1, 'in_progress', now() + interval '60 seconds')"
        )
    conn.commit()


def test_fresh_init_only_one_in_progress_attempt_per_application(conn):
    _build_fresh_init(conn)
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {FRESH_SCHEMA}")
        cur.execute("INSERT INTO applications (id, amount, term_months, status) VALUES (1, 9000, 24, 'submitted')")
        cur.execute(
            "INSERT INTO decision_attempts (app_id, state, lease_expires_at) "
            "VALUES (1, 'in_progress', now() + interval '60 seconds')"
        )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {FRESH_SCHEMA}")
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO decision_attempts (app_id, state, lease_expires_at) "
                "VALUES (1, 'in_progress', now() + interval '60 seconds')"
            )
    conn.rollback()


def test_fresh_init_one_decision_event_per_attempt(conn):
    _build_fresh_init(conn)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {FRESH_SCHEMA}")
        cur.execute("INSERT INTO applications (id, amount, term_months, status) VALUES (1, 9000, 24, 'approved')")
        cur.execute(
            "INSERT INTO decision_attempts (app_id, state, lease_expires_at) "
            "VALUES (1, 'in_progress', now() + interval '60 seconds') RETURNING id"
        )
        attempt_id = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO decision_events (app_id, model_version, decision, attempt_id) "
            "VALUES (1, 'v1', 'approve', %s)",
            (attempt_id,),
        )
    conn.commit()

    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {FRESH_SCHEMA}")
        with pytest.raises(psycopg2.errors.UniqueViolation):
            cur.execute(
                "INSERT INTO decision_events (app_id, model_version, decision, attempt_id) "
                "VALUES (1, 'v1', 'approve', %s)",
                (attempt_id,),
            )
    conn.rollback()


def test_migration_0023_adds_decision_attempts_and_preserves_history(conn):
    _build_pre_0023_schema_with_history(conn)
    events_before = _columns(conn, MIGRATED_SCHEMA, "decision_events")
    assert "attempt_id" not in events_before  # sanity: the pre-fix shape really existed

    _run_sql_file(conn, MIGRATED_SCHEMA, MIGRATIONS_DIR / "0023_decision_attempts.sql")
    _run_sql_file(conn, MIGRATED_SCHEMA, MIGRATIONS_DIR / "0024_decision_attempt_bureau_key.sql")

    events_after = _columns(conn, MIGRATED_SCHEMA, "decision_events")
    assert "attempt_id" in events_after

    attempt_cols = _columns(conn, MIGRATED_SCHEMA, "decision_attempts")
    assert "state" in attempt_cols
    assert "lease_expires_at" in attempt_cols

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {MIGRATED_SCHEMA}")
        cur.execute("SELECT decision, model_version, reason_codes FROM decision_events WHERE app_id = 1")
        row = cur.fetchone()
        assert row["decision"] == "approve"
        assert row["model_version"] == "v1"
        # Historical row keeps attempt_id NULL forever -- the append-only
        # trigger forbids ever backfilling it, and the migration must not
        # attempt to.
        cur.execute("SELECT attempt_id FROM decision_events WHERE app_id = 1")
        assert cur.fetchone()["attempt_id"] is None


def test_fresh_init_and_migrated_schemas_agree_on_decision_attempts(conn):
    _build_fresh_init(conn)
    _build_pre_0023_schema_with_history(conn)
    _run_sql_file(conn, MIGRATED_SCHEMA, MIGRATIONS_DIR / "0023_decision_attempts.sql")
    _run_sql_file(conn, MIGRATED_SCHEMA, MIGRATIONS_DIR / "0024_decision_attempt_bureau_key.sql")

    fresh_cols = _columns(conn, FRESH_SCHEMA, "decision_attempts")
    migrated_cols = _columns(conn, MIGRATED_SCHEMA, "decision_attempts")
    assert fresh_cols == migrated_cols, (
        f"decision_attempts differs between fresh-init and migrated: "
        f"{fresh_cols} vs {migrated_cols}"
    )

    fresh_events_cols = _columns(conn, FRESH_SCHEMA, "decision_events")
    migrated_events_cols = _columns(conn, MIGRATED_SCHEMA, "decision_events")
    assert fresh_events_cols["attempt_id"] == migrated_events_cols["attempt_id"]

    fresh_indexes = _indexes(conn, FRESH_SCHEMA, "decision_attempts")
    migrated_indexes = _indexes(conn, MIGRATED_SCHEMA, "decision_attempts")
    assert "idx_decision_attempts_one_active" in fresh_indexes
    assert "idx_decision_attempts_one_active" in migrated_indexes


def test_every_seeded_loan_carries_its_contractual_schedule():
    """A fresh database must not look like a pile of legacy loans.

    The loan inserts in db/init predate the Model B schedule columns, so on a
    fresh `docker compose up` every seeded loan had NULL
    regular_payment/count/final_payment/schedule_version. Servicing then hid
    each loan's note rate -- nothing proved `loans.apr` held a contractual rate
    -- and rendered its schedule as a reconstruction, on a database whose offers
    contain an exact B1 contract. Migrations are not replayed over fresh init,
    so 003_seed_bulk.sql copies each loan's schedule from its own offer.
    Reviewed on PR #10.
    """
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set")
    conn = psycopg2.connect(DATABASE_URL)
    try:
        with conn.cursor() as cur:
            cur.execute("SET search_path TO public")
            # CI runs this suite against a BARE database -- db/init is never
            # applied there, so the seeded tables do not exist. This assertion
            # is about the seed, so it has nothing to say on that database.
            cur.execute("SELECT to_regclass('public.loans') IS NOT NULL")
            if not cur.fetchone()[0]:
                pytest.skip("no seeded schema in this database (db/init not applied)")
            cur.execute("SELECT count(*) FROM loans")
            total = cur.fetchone()[0]
            if total == 0:
                pytest.skip("no seeded loans in this database")
            cur.execute(
                "SELECT count(*) FROM loans l JOIN offers o ON o.app_id = l.app_id "
                "WHERE o.schedule_version IS NOT NULL AND l.schedule_version IS NULL"
            )
            unproven = cur.fetchone()[0]
        assert unproven == 0, (
            f"{unproven} seeded loan(s) have an offer with a stored contract but no "
            f"schedule of their own -- servicing will hide their note rate and "
            f"label their schedule an estimate"
        )
    finally:
        conn.close()
