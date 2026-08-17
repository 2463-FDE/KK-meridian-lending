"""D19 contract: dropping `loans.apr`, the column whose name was the defect.

0038 added `note_rate_pct` and back-filled it only where the value could be
proven. This migration removes the old name -- and it destroys data, so almost
every case below is about what it REFUSES to do.

The refusals are the feature. For a loan whose rate was never proven, `apr` is
the only rate the row carries and the legacy schedule is reconstructed from it;
dropping it there does not rename anything, it removes a borrower's ability to
see what they owe. So gate 1 refuses while any such row exists, and the tests
that matter most are the ones asserting it still refuses under pressure: with
the operator acknowledgement already set, with only one bad row in a thousand.

Against real PostgreSQL. The gates are `DO $$` blocks raising exceptions inside
a transaction, and the thing being asserted is that the schema is UNCHANGED
afterwards -- a mock cannot fail to roll back.
"""
import os
import pathlib

import psycopg2
import psycopg2.extras
import pytest

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against"
)

REPO = pathlib.Path(__file__).resolve().parents[2]
INIT = REPO / "db" / "init"
MIGRATION = REPO / "db" / "migrations" / "0039_drop_loans_apr.sql"
SCHEMA = "apr_contract_test"
FRESH_SCHEMA = "apr_contract_fresh"
INIT_FILES = ("001_schema.sql", "004_decision_events.sql", "005_manual_reviews.sql",
              "006_decision_attempts.sql", "007_ledger_opening_balances.sql")


def _exec(conn, sql, params=None, schema=SCHEMA):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {schema}")
        cur.execute(sql, params or ())
        return cur.fetchall() if cur.description else []


def _build(conn, schema):
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        cur.execute(f"CREATE SCHEMA {schema}")
    conn.commit()
    for name in INIT_FILES:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute((INIT / name).read_text(encoding="utf-8"))
        conn.commit()


@pytest.fixture
def db():
    """A database at the state BEFORE 0039, reconstructed from the current tree.

    `db/init/001_schema.sql` is the state AFTER this migration -- no `apr`, and
    `note_rate_pct NOT NULL`. The state 0039 operates on is the post-0038 one:
    both columns present, the new one still nullable because 0038 deliberately
    left unprovable rows NULL. So the two differences are put back here.

    That reconstruction is itself worth stating plainly: it means these tests
    prove the migration against a schema this repository builds, not against the
    schema a real legacy database is actually in. What closes that gap is
    `test_the_migrated_and_fresh_schemas_agree`, which checks the shape 0039
    produces against the shape `db/init` ships.
    """
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    _build(conn, SCHEMA)
    _exec(conn, "ALTER TABLE loans ADD COLUMN apr NUMERIC(7,3)")
    _exec(conn, "ALTER TABLE loans ALTER COLUMN note_rate_pct DROP NOT NULL")
    conn.commit()
    yield conn
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"DROP SCHEMA IF EXISTS {FRESH_SCHEMA} CASCADE")
    conn.commit()
    conn.close()


def _apply(conn):
    """The migration as shipped, minus its own BEGIN/COMMIT (psycopg2 owns the
    transaction; a nested BEGIN is a warning and a lie about what is atomic)."""
    sql = MIGRATION.read_text(encoding="utf-8").replace("BEGIN;", "", 1)
    sql = "".join(sql.rsplit("COMMIT;", 1))
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(sql)
    conn.commit()


def _acknowledge(conn, value="yes"):
    _exec(conn, "SET meridian.loans_apr_drop_acknowledged = %s", (value,))


def _loan(conn, *, apr=None, note_rate_pct=None, name="T",
          principal="18000.00", term=48):
    applicant = _exec(conn, "INSERT INTO applicants (name) VALUES (%s) RETURNING id",
                      (name,))[0]["id"]
    app = _exec(conn, "INSERT INTO applications (applicant_id, amount, term_months, "
                      "status) VALUES (%s, %s, %s, 'funded') RETURNING id",
                (applicant, principal, term))[0]["id"]
    return _exec(conn,
                 "INSERT INTO loans (app_id, applicant_name, principal, apr, "
                 "note_rate_pct, term_months) "
                 "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
                 (app, name, principal, apr, note_rate_pct, term))[0]["id"]


def _columns(conn, table, schema=SCHEMA):
    return {r["column_name"]: r for r in _exec(
        conn,
        "SELECT column_name, is_nullable, data_type, numeric_precision, "
        "       numeric_scale "
        "  FROM information_schema.columns "
        " WHERE table_schema = %s AND table_name = %s",
        (schema, table), schema=schema)}


# --- gate 1: an unproven rate blocks the drop --------------------------------

def test_it_refuses_while_any_loan_has_no_proven_note_rate(db):
    """The refusal that protects the borrower who cannot otherwise be billed."""
    _loan(db, apr=5.196, note_rate_pct=None, name="unproven")
    db.commit()

    # Guard the guard: the fixture must actually have produced the state under
    # test. A NOT NULL that had survived the fixture would make this vacuous.
    assert _exec(db, "SELECT count(*) c FROM loans WHERE note_rate_pct IS NULL"
                 )[0]["c"] == 1

    _acknowledge(db)
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _apply(db)
    db.rollback()

    assert "no proven note_rate_pct" in str(exc.value)


def test_the_refusal_names_the_loans_that_caused_it(db):
    """An operator who cannot see WHICH rows blocked it has to go find them, and
    the obvious way to go find them is to copy `apr` across -- the one action the
    migration exists to prevent."""
    ids = [_loan(db, apr=5.196, note_rate_pct=None, name=f"u{i}") for i in range(3)]
    _loan(db, apr=7.99, note_rate_pct=7.99, name="proven")
    db.commit()

    _acknowledge(db)
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _apply(db)
    db.rollback()

    message = str(exc.value)
    assert "3 loan(s)" in message
    for loan_id in ids:
        assert str(loan_id) in message


def test_the_refusal_warns_against_copying_apr_across(db):
    """The message has to say why the obvious repair is wrong, not only that it
    stopped. `apr` on a pre-change loan is the DISCLOSED APR; recording that as
    the contractual term states something the borrower never agreed to."""
    _loan(db, apr=5.196, note_rate_pct=None)
    db.commit()

    _acknowledge(db)
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _apply(db)
    db.rollback()

    message = str(exc.value)
    assert "Do NOT copy" in message
    assert "DISCLOSED APR" in message


def test_the_acknowledgement_does_not_override_the_unproven_check(db):
    """The two gates are not alternatives. An operator holding the release
    acknowledgement must still not be able to drop a rate nobody can reconstruct
    -- gate 1 runs first and is not waivable by anything in this file."""
    _loan(db, apr=5.196, note_rate_pct=None)
    db.commit()

    _acknowledge(db, "yes")
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _apply(db)
    db.rollback()

    assert "no proven note_rate_pct" in str(exc.value), (
        "the acknowledgement bypassed the unproven-rate gate"
    )


def test_one_unproven_loan_among_many_still_blocks_it(db):
    """Not a threshold. One borrower who cannot see their schedule is the whole
    reason for the gate, and a percentage-based check would ship exactly that."""
    for i in range(20):
        _loan(db, apr=7.99, note_rate_pct=7.99, name=f"ok{i}")
    _loan(db, apr=5.196, note_rate_pct=None, name="the one")
    db.commit()

    _acknowledge(db)
    with pytest.raises(psycopg2.errors.RaiseException):
        _apply(db)
    db.rollback()


def test_a_refused_migration_leaves_the_column_in_place(db):
    """The refusal has to be transactional. A gate that raises AFTER the DROP
    would report failure and have already destroyed the data."""
    _loan(db, apr=5.196, note_rate_pct=None)
    db.commit()

    _acknowledge(db)
    with pytest.raises(psycopg2.errors.RaiseException):
        _apply(db)
    db.rollback()

    columns = _columns(db, "loans")
    assert "apr" in columns, "a refused migration had already dropped the column"
    assert _exec(db, "SELECT apr FROM loans")[0]["apr"] is not None


# --- gate 2: the operator acknowledgement ------------------------------------

def test_it_refuses_without_the_operator_acknowledgement(db):
    """Every rate is proven and it still will not run. The second gate is about
    the running fleet, not the data: an image that still SELECTs `apr` starts
    erroring the moment the column goes."""
    _loan(db, apr=7.99, note_rate_pct=7.99)
    db.commit()

    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _apply(db)
    db.rollback()

    assert "loans_apr_drop_acknowledged" in str(exc.value)
    assert "apr" in _columns(db, "loans")


def test_a_near_miss_acknowledgement_is_not_an_acknowledgement(db):
    """`'true'`, `'YES'` and `'y'` are what a hurried operator types. The check is
    an exact match against 'yes' so that a half-remembered value fails closed
    rather than authorising a destructive migration."""
    _loan(db, apr=7.99, note_rate_pct=7.99)
    db.commit()

    for wrong in ("true", "YES", "y", "1", ""):
        _acknowledge(db, wrong)
        with pytest.raises(psycopg2.errors.RaiseException) as exc:
            _apply(db)
        db.rollback()
        assert "loans_apr_drop_acknowledged" in str(exc.value), (
            f"{wrong!r} was accepted as an acknowledgement"
        )


def test_an_unset_acknowledgement_is_not_an_error(db):
    """`current_setting(..., true)` -- the missing-ok form. Without it the
    migration dies on an undefined GUC and reports something other than the
    refusal it means, which is how an operator concludes the migration is broken
    and starts editing it."""
    _loan(db, apr=7.99, note_rate_pct=7.99)
    db.commit()

    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _apply(db)
    db.rollback()

    assert "unrecognized configuration parameter" not in str(exc.value)


# --- the drop itself ---------------------------------------------------------

def test_it_drops_apr_once_both_gates_are_satisfied(db):
    _loan(db, apr=7.99, note_rate_pct=7.99)
    db.commit()

    _acknowledge(db)
    _apply(db)

    columns = _columns(db, "loans")
    assert "apr" not in columns
    assert "note_rate_pct" in columns


def test_the_surviving_rate_keeps_its_value(db):
    """A drop that also disturbed the column it leaves behind would be the
    silent version of the defect."""
    _loan(db, apr=5.196, note_rate_pct=7.99)
    db.commit()

    _acknowledge(db)
    _apply(db)

    assert float(_exec(db, "SELECT note_rate_pct FROM loans")[0]["note_rate_pct"]) == 7.99


def test_note_rate_becomes_not_null(db):
    _loan(db, apr=7.99, note_rate_pct=7.99)
    db.commit()

    _acknowledge(db)
    _apply(db)

    assert _columns(db, "loans")["note_rate_pct"]["is_nullable"] == "NO"


def test_a_boarding_path_that_forgets_the_rate_now_fails_at_the_insert(db):
    """What the NOT NULL is FOR. Before it, a new boarding path that omitted the
    column created a loan whose contractual rate was unknown -- the exact state
    0038 and 0039 exist to clear up, re-entered from the front door."""
    _loan(db, apr=7.99, note_rate_pct=7.99)
    db.commit()

    _acknowledge(db)
    _apply(db)

    applicant = _exec(db, "INSERT INTO applicants (name) VALUES ('X') RETURNING id"
                      )[0]["id"]
    app = _exec(db, "INSERT INTO applications (applicant_id, amount, term_months, "
                    "status) VALUES (%s, 1000, 12, 'funded') RETURNING id",
                (applicant,))[0]["id"]

    with pytest.raises(psycopg2.errors.NotNullViolation):
        _exec(db, "INSERT INTO loans (app_id, applicant_name, principal, term_months) "
                  "VALUES (%s, 'X', 1000, 12)", (app,))
    db.rollback()


def test_an_empty_database_migrates(db):
    """A fresh install has no loans, so gate 1's count is zero. Failing there
    would make an empty database unbootable -- the same trap 0038's assertion
    had to avoid."""
    assert _exec(db, "SELECT count(*) c FROM loans")[0]["c"] == 0

    _acknowledge(db)
    _apply(db)

    assert "apr" not in _columns(db, "loans")


def test_it_is_rerunnable(db):
    """`DROP COLUMN IF EXISTS`. A migration runner that replays the file must not
    turn a completed migration into a failed one."""
    _loan(db, apr=7.99, note_rate_pct=7.99)
    db.commit()

    _acknowledge(db)
    _apply(db)
    _apply(db)

    assert "apr" not in _columns(db, "loans")


def test_the_column_comment_says_which_figure_it_holds(db):
    """The name is only half the fix. The comment is what a reader meets in a
    dump or in `\\d loans`, and it is where the distinction from the disclosed
    APR has to survive."""
    _loan(db, apr=7.99, note_rate_pct=7.99)
    db.commit()

    _acknowledge(db)
    _apply(db)

    comment = _exec(db,
                    "SELECT col_description(%s::regclass, ordinal_position) AS c "
                    "  FROM information_schema.columns "
                    " WHERE table_schema = %s AND table_name = 'loans' "
                    "   AND column_name = 'note_rate_pct'",
                    (f"{SCHEMA}.loans", SCHEMA))[0]["c"]

    assert comment and "contractual" in comment
    assert "disclosed APR" in comment


# --- the two schema paths have to agree --------------------------------------

def test_the_migrated_and_fresh_schemas_agree(db):
    """The parity check the fixture's reconstruction depends on.

    A migration is only half the change: `db/init/001_schema.sql` builds a NEW
    database directly, and if the two paths disagree, a developer's fresh
    checkout and production are running different schemas -- which is how a
    migration passes every test here and breaks on deploy.
    """
    _loan(db, apr=7.99, note_rate_pct=7.99)
    db.commit()
    _acknowledge(db)
    _apply(db)

    _build(db, FRESH_SCHEMA)

    migrated = _columns(db, "loans")
    fresh = _columns(db, "loans", schema=FRESH_SCHEMA)

    assert set(migrated) == set(fresh), (
        "the migrated and freshly-built `loans` tables have different columns"
    )
    for name in ("note_rate_pct",):
        assert migrated[name]["is_nullable"] == fresh[name]["is_nullable"]
        assert migrated[name]["numeric_precision"] == fresh[name]["numeric_precision"]
        assert migrated[name]["numeric_scale"] == fresh[name]["numeric_scale"]


def test_the_fresh_schema_has_no_apr_column(db):
    """Stated separately from the parity check because it is the D19 claim
    itself: nobody reading `db/init` meets a column called `apr` that holds a
    note rate. Parity alone would be satisfied by both paths being wrong."""
    _build(db, FRESH_SCHEMA)
    assert "apr" not in _columns(db, "loans", schema=FRESH_SCHEMA)


def test_offers_apr_is_untouched(db):
    """`offers.apr` is a REAL disclosed APR and stays. The scope of this change
    is the loan row, where the name was wrong -- dropping the offer's column
    would delete the one figure that is correctly named."""
    _acknowledge(db)
    _apply(db)

    assert "apr" in _columns(db, "offers")
