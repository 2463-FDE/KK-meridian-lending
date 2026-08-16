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
SCHEMAS = {
    "fresh": "gapd_fresh_init",
    "legacy": "gapd_legacy_then_migrations",
    "replay": "gapd_fresh_then_migrations",
    "twice": "gapd_migrations_twice",
}


from migration_paths import (  # the one real implementation -- see that module
    INIT_DIR,
    INIT_SCHEMA_FILES,
    MIGRATIONS_DIR,
    _all_migrations,
    _apply_all_migrations,
    _build_fresh_init,
    _build_legacy_schema,
    _has_executable_sql,
    _run_sql,
)


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



# --- helpers to compare shapes ------------------------------------------------

def _columns(conn, schema, table):
    """Column name -> (fully qualified type, nullability).

    `format_type(atttypid, atttypmod)` rather than information_schema's
    `data_type`, because that view reports both `NUMERIC(14,2)` and
    unconstrained `NUMERIC` as plain "numeric" -- the precision and scale live
    in `numeric_precision`/`numeric_scale`, which the old comparison never read.
    Two schemas could therefore "converge" while one enforced money to the cent
    and the other accepted arbitrary precision.

    In a lending schema that typmod is the data-integrity contract, not
    cosmetic metadata: it is what stops a rate or a balance being stored at a
    precision the rest of the system does not expect. The same call also covers
    `varchar(n)` length and timestamp precision, which had the same blind spot.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT a.attname AS column_name, "
            "       format_type(a.atttypid, a.atttypmod) AS full_type, "
            "       CASE WHEN a.attnotnull THEN 'NO' ELSE 'YES' END AS is_nullable "
            "  FROM pg_attribute a "
            "  JOIN pg_class c ON c.oid = a.attrelid "
            "  JOIN pg_namespace n ON n.oid = c.relnamespace "
            " WHERE n.nspname = %s AND c.relname = %s "
            "   AND a.attnum > 0 AND NOT a.attisdropped "
            " ORDER BY a.attname",
            (schema, table),
        )
        return {r["column_name"]: (r["full_type"], r["is_nullable"]) for r in cur.fetchall()}


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


def _checks(conn, schema, table):
    """CHECK constraints as {name: (normalized expression, convalidated)}.

    Review finding: comparing only columns and UNIQUE constraints let two real
    divergences through. A CHECK present on one path and absent on another is a
    rule the database enforces for some operators and not others; and a CHECK
    that is VALIDATED on a fresh volume but NOT VALID after a replay is a
    *silently weaker* database that looks identical to a column-level diff --
    exactly the downgrade 0026's conditional add exists to prevent.

    NOT NULL is excluded: it surfaces as `is_nullable` in _columns() already.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT c.conname, pg_get_constraintdef(c.oid) AS def, c.convalidated "
            "FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "WHERE n.nspname = %s AND t.relname = %s AND c.contype = 'c'",
            (schema, table),
        )
        # pg_get_constraintdef appends " NOT VALID" for unvalidated constraints;
        # convalidated already carries that, so strip it to compare the RULE and
        # its validation state as two independent facts.
        return {
            r["conname"]: (r["def"].replace(" NOT VALID", ""), r["convalidated"])
            for r in cur.fetchall()
        }


def _foreign_keys(conn, schema, table):
    """FKs keyed by (referencing columns, referenced table) -- not by name, for
    the same reason UNIQUEs are not: the names are auto-generated and a rename
    is not a schema difference. The referenced table is compared unqualified so
    a schema-qualified definition still matches."""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT pg_get_constraintdef(c.oid) AS def, ft.relname AS ref_table, "
            "       (SELECT array_agg(a.attname ORDER BY a.attname) "
            "          FROM pg_attribute a "
            "         WHERE a.attrelid = t.oid AND a.attnum = ANY(c.conkey)) AS cols "
            "FROM pg_constraint c "
            "JOIN pg_class t ON t.oid = c.conrelid "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "JOIN pg_class ft ON ft.oid = c.confrelid "
            "WHERE n.nspname = %s AND t.relname = %s AND c.contype = 'f'",
            (schema, table),
        )
        return {(tuple(r["cols"]), r["ref_table"]) for r in cur.fetchall()}


def _indexes(conn, schema, table):
    """Index definitions with the schema qualifier and index name stripped, so
    two paths that built the same index under different auto-generated names
    still compare equal. A missing partial unique index (payments'
    idempotency_key, for one) is a lost guarantee, not a cosmetic difference."""
    import re

    with conn.cursor() as cur:
        cur.execute(
            "SELECT indexdef FROM pg_indexes WHERE schemaname = %s AND tablename = %s",
            (schema, table),
        )
        out = set()
        for (indexdef,) in cur.fetchall():
            normalized = re.sub(r"^CREATE (UNIQUE )?INDEX \S+ ON \S+", r"CREATE \1INDEX ON", indexdef)
            out.add(normalized.replace(f"{schema}.", ""))
        return out


def _defaults(conn, schema, table):
    """Column defaults. A DEFAULT that exists on one path and not another means
    rows written through the same code end up different depending on how the
    operator's database was built. Sequence defaults are normalized: nextval()
    embeds the schema-qualified sequence name, which differs by construction."""
    import re

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT column_name, column_default FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s AND column_default IS NOT NULL",
            (schema, table),
        )
        return {
            r["column_name"]: re.sub(r"nextval\('[^']+'", "nextval('<seq>'", r["column_default"])
            for r in cur.fetchall()
        }


def _triggers(conn, schema, table):
    """Triggers on the table, with the function each one calls and when it fires.

    **This aspect was missing, and its absence hid a real divergence.**
    `db/migrations/0035` creates three triggers on `balances` -- the ledger
    compatibility bridge, the balance/ledger parity check, and the guard against
    deleting a projection row. `db/init` created none of them, so a freshly
    built database had the ledger tables and the projection but none of the
    controls that keep the projection honest. Every other aspect matched, so
    every case here passed.

    A trigger IS the control in this schema: append-only, parity and the
    projection itself are all triggers. Comparing columns and constraints while
    ignoring triggers compares the shape of the database and not its rules.

    Timing and events are compared as well as the name, because a trigger that
    fires `BEFORE UPDATE` on one path and `AFTER UPDATE` on the other is a
    different control wearing the same name -- and `DEFERRABLE` matters more
    still: a constraint trigger checked per statement instead of at commit
    forbids ordinary work that is in balance by the time it commits.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT t.tgname, p.proname, t.tgtype, t.tgdeferrable, t.tginitdeferred "
            "  FROM pg_trigger t "
            "  JOIN pg_class c ON c.oid = t.tgrelid "
            "  JOIN pg_namespace n ON n.oid = c.relnamespace "
            "  JOIN pg_proc p ON p.oid = t.tgfoid "
            " WHERE n.nspname = %s AND c.relname = %s AND NOT t.tgisinternal",
            (schema, table),
        )
        return {
            r["tgname"]: (r["proname"], r["tgtype"], r["tgdeferrable"],
                          r["tginitdeferred"])
            for r in cur.fetchall()
        }


def _shape(conn, schema, table):
    """Everything compared for a table, as one value."""
    return {
        "columns": _columns(conn, schema, table),
        "unique_columns": _unique_columns(conn, schema, table),
        "checks": _checks(conn, schema, table),
        "foreign_keys": _foreign_keys(conn, schema, table),
        "indexes": _indexes(conn, schema, table),
        "defaults": _defaults(conn, schema, table),
        "triggers": _triggers(conn, schema, table),
    }


def _functions(conn, schema):
    """Every function in the schema, by name and argument signature.

    Schema-wide rather than per-table, because a trigger function is not owned
    by a table and a missing one is invisible in any per-table comparison. Two
    of the three divergences this file now catches were missing FUNCTIONS
    (`capture_legacy_balance_delta`, `balances_cannot_be_deleted_during_cutover`)
    as well as missing triggers.

    Bodies are deliberately NOT compared. `db/init` and `db/migrations` are
    independent copies of the same definitions -- 007 says so about itself -- and
    holding them to byte-identical bodies would fail on a reformatted comment
    while still passing on a control that was never attached to anything. The
    name and signature are what a trigger definition depends on; whether the
    control actually fires is what the trigger comparison above tests.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT p.proname, pg_get_function_identity_arguments(p.oid) AS args "
            "  FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace "
            " WHERE n.nspname = %s",
            (schema,),
        )
        return {(r["proname"], r["args"]) for r in cur.fetchall()}


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

def _tables(conn, schema):
    """Every ordinary table in the schema, derived rather than listed.

    **The list this replaces omitted `balances`, `ledger_entries` and
    `pending_movements`** -- the money tables, and the ones carrying the ledger
    controls. So no comparison in this file ever looked at them, and a
    divergence there would have been invisible however many aspects were added.

    That is the same defect this repository keeps producing in a new place: a
    hand-maintained list that reads complete while missing one. `db/init` is the
    definition of what a database has, so the tables to compare come from it.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT c.relname FROM pg_class c "
            "  JOIN pg_namespace n ON n.oid = c.relnamespace "
            " WHERE n.nspname = %s AND c.relkind = 'r' ORDER BY c.relname",
            (schema,),
        )
        return tuple(r[0] for r in cur.fetchall())

# Both comparisons derive their tables from the fresh schema (see `_tables`),
# so neither can be narrowed by editing a list -- and narrowing one can still
# never silently narrow the other, because neither is a list any more.


def _build_all_paths(conn):
    _build_fresh_init(conn, SCHEMAS["fresh"])

    _build_fresh_init(conn, SCHEMAS["replay"])
    _apply_all_migrations(conn, SCHEMAS["replay"])

    _build_fresh_init(conn, SCHEMAS["twice"])
    _apply_all_migrations(conn, SCHEMAS["twice"])
    _apply_all_migrations(conn, SCHEMAS["twice"])

    _build_legacy_schema(conn, SCHEMAS["legacy"])
    _apply_all_migrations(conn, SCHEMAS["legacy"])


@pytest.mark.parametrize("aspect", ["columns", "unique_columns", "checks",
                                    "foreign_keys", "indexes", "defaults",
                                    "triggers"])
@pytest.mark.parametrize("other", ["replay", "twice"])
def test_init_based_paths_converge_on_every_aspect(conn, other, aspect):
    """Fresh init, fresh init + migrations, and fresh init + migrations twice
    must agree on columns, UNIQUEs, CHECKs (rule AND validation state), foreign
    keys, indexes and defaults -- one parametrized case per aspect so a failure
    names which kind of divergence it is."""
    _build_all_paths(conn)
    tables = _tables(conn, SCHEMAS["fresh"])
    assert len(tables) > 8, (
        f"only {len(tables)} tables found on the fresh path -- the comparison "
        f"is not reading the schema, so a pass here proves nothing"
    )
    for table in tables:
        expected = _shape(conn, SCHEMAS["fresh"], table)[aspect]
        actual = _shape(conn, SCHEMAS[other], table)[aspect]
        assert actual == expected, f"{table}.{aspect} diverges between fresh-init and {other}"


def test_a_replay_never_downgrades_a_validated_check_to_not_valid(conn):
    """0026 adds offers_canonical_terms_present NOT VALID for an operator who
    may still hold damaged rows. On a fresh volume the same constraint is
    already VALIDATED, and a replay must leave it that way -- a drop-and-re-add
    would silently weaken that database while every column still matched."""
    _build_all_paths(conn)
    for path in ("fresh", "replay", "twice"):
        checks = _checks(conn, SCHEMAS[path], "offers")
        assert "offers_canonical_terms_present" in checks, f"missing on the {path} path"
        _, validated = checks["offers_canonical_terms_present"]
        assert validated is True, f"downgraded to NOT VALID on the {path} path"


@pytest.mark.parametrize("aspect", ["columns", "unique_columns", "checks",
                                    "foreign_keys", "indexes", "defaults",
                                    "triggers"])
def test_legacy_upgrade_reaches_the_same_shape_as_a_fresh_install(conn, aspect):
    """The path an existing operator actually takes, compared against the path a
    new one gets. This is the comparison that matters most and was missing: an
    upgraded database that is merely "close" to a fresh one is a database where
    a rule holds for some deployments and not others.

    Scoped to the tables the migrations are responsible for bringing up to date.
    Tables the legacy fixture never had (payment_applications, decision_events,
    manual_reviews, decision_attempts) are created wholesale by migrations, so
    they are in scope too -- if a migration builds one differently from db/init,
    that is exactly the drift being looked for.
    """
    _build_all_paths(conn)
    tables = _tables(conn, SCHEMAS["fresh"])
    assert len(tables) > 8, (
        f"only {len(tables)} tables found on the fresh path -- the comparison "
        f"is not reading the schema, so a pass here proves nothing"
    )
    for table in tables:
        expected = _shape(conn, SCHEMAS["fresh"], table)[aspect]
        actual = _shape(conn, SCHEMAS["legacy"], table)[aspect]
        assert actual == expected, (
            f"{table}.{aspect} diverges between a fresh install and a legacy upgrade"
        )


@pytest.mark.parametrize("other", ["replay", "twice", "legacy"])
def test_every_path_defines_the_same_functions(conn, other):
    """Schema-wide, because a trigger function belongs to no table.

    The per-table comparisons above cannot see a missing function: they look at
    tables, and `capture_legacy_balance_delta` is not on one. Both of the
    functions `db/init` was missing -- that one and
    `balances_cannot_be_deleted_during_cutover` -- would have stayed invisible
    to every other case in this file.

    Names and signatures only, not bodies. `db/init` and `db/migrations` are
    independent copies of the same definitions on purpose (007 says so about
    itself), so requiring identical bodies would fail on a reworded comment
    while still passing on a control nothing had attached. Whether the control
    fires is the `triggers` aspect's job.
    """
    _build_all_paths(conn)
    expected = _functions(conn, SCHEMAS["fresh"])
    actual = _functions(conn, SCHEMAS[other])

    # Guard the guard: an empty comparison would pass and prove nothing.
    assert len(expected) > 5, (
        f"only {len(expected)} functions found on the fresh path -- the sweep is "
        f"not reading the schema"
    )
    missing = sorted(n for n, _ in expected - actual)
    extra = sorted(n for n, _ in actual - expected)
    assert not missing and not extra, (
        f"functions diverge between a fresh install and {other} -- "
        f"missing from {other}: {missing or 'none'}; "
        f"present only on {other}: {extra or 'none'}"
    )


def test_the_ledger_controls_exist_on_a_fresh_install(conn):
    """Named individually, because this is the divergence that prompted the two
    comparisons above and a regression here should say so by name.

    A fresh database had the ledger tables and `project_ledger_entry` but none
    of the three controls on `balances`. The consequence was not theoretical: the
    compatibility bridge is what makes an unconverted legacy writer *recorded*
    rather than *invisible*, and it was absent on exactly the path every
    developer and every e2e run uses.
    """
    _build_fresh_init(conn, SCHEMAS["fresh"])
    triggers = _triggers(conn, SCHEMAS["fresh"], "balances")

    for name in ("balances_capture_legacy_delta",
                 "balances_ledger_parity",
                 "balances_reject_delete_during_cutover"):
        assert name in triggers, (
            f"{name} is missing from a freshly built database. ADR 0010's "
            f"controls hold on the migrated path only, so the guarantee depends "
            f"on how the operator's database was built."
        )

    # The parity check must be DEFERRABLE INITIALLY DEFERRED. Attached as an
    # ordinary constraint it would fire per statement and refuse a transaction
    # that is in balance by the time it commits -- the same control by name,
    # rejecting correct work.
    _, _, deferrable, initially_deferred = triggers["balances_ledger_parity"]
    assert deferrable and initially_deferred, (
        "balances_ledger_parity is not DEFERRABLE INITIALLY DEFERRED"
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


def test_the_column_comparison_actually_sees_numeric_precision(conn):
    """Proof that the convergence assertions can fail on precision.

    Every path test above passes, which is only meaningful if the comparison
    could have failed. It could not before: information_schema reports both
    NUMERIC(14,2) and unconstrained NUMERIC as "numeric", so a legacy upgrade
    enforcing money to the cent and a fresh install accepting arbitrary
    precision compared equal -- and in a lending schema that typmod is the
    data-integrity contract.

    Two tables, one difference, asserted visible.
    """
    schema = SCHEMAS["fresh"]
    _run_sql(conn, schema, "CREATE TABLE precise_money (m NUMERIC(14,2));")
    _run_sql(conn, schema, "CREATE TABLE loose_money (m NUMERIC);")

    precise = _columns(conn, schema, "precise_money")
    loose = _columns(conn, schema, "loose_money")

    assert precise["m"] != loose["m"], (
        f"NUMERIC(14,2) and NUMERIC compare equal ({precise['m']}), so the "
        "convergence tests cannot detect a money-precision divergence"
    )
    assert "14,2" in precise["m"][0].replace(" ", "")
