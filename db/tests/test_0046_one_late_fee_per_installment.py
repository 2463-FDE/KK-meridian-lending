"""db/migrations/0046 -- one late fee per installment, enforced by the database.

`docs/DEBT.md` D23 names this as the part of the fix that has to live in the
schema rather than in application code: a unique index makes the concurrent
assessor and the retry safe *by construction*, where an application check makes
them safe only while every writer remembers to take the same lock.

Against real PostgreSQL, in a throwaway schema built from `db/init` and then
migrated with 0046 -- the same shape as `test_0035_ledger_projection.py`. That
makes these migration-path tests as well as constraint tests: `db/init` already
carries the column (backported, as this repository does for every migration), so
applying 0046 on top of it proves the two agree and that the migration is
re-runnable rather than only correct once.
"""
import os
import pathlib

import psycopg2
import psycopg2.extras
import pytest

DATABASE_URL = os.getenv("DATABASE_URL")

REPO = pathlib.Path(__file__).resolve().parents[2]
SCHEMA = "installment_fee_test"
INIT = REPO / "db" / "init"
MIGRATION = REPO / "db" / "migrations" / "0046_ledger_installment_no.sql"
INIT_FILES = ("001_schema.sql", "002_seed.sql", "003_seed_bulk.sql",
              "004_decision_events.sql", "005_manual_reviews.sql",
              "006_decision_attempts.sql",
              "007_ledger_opening_balances.sql")


def _apply_migration(conn):
    """The migration as shipped, minus its own BEGIN/COMMIT.

    psycopg2 manages the transaction, and a nested BEGIN inside one is a warning
    and a lie about what is atomic. The statements are unchanged.
    """
    sql = MIGRATION.read_text(encoding="utf-8")
    sql = sql.replace("BEGIN;", "", 1)
    sql = "".join(sql.rsplit("COMMIT;", 1))
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(sql)
    conn.commit()


def _connect():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
    conn.commit()
    return conn


@pytest.fixture(scope="module")
def db():
    """A throwaway schema with `db/init` applied and 0046 migrated on top.

    Module-scoped and skipped on the FIXTURE rather than the module, so the one
    case here that reads the migration text instead of the database still runs
    without a Postgres. A module-level `pytestmark` skipped it too, which left the
    no-backfill guarantee -- the part needing no database at all -- unchecked on
    any run without `DATABASE_URL`.
    """
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set -- no Postgres to test against")
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
            cur.execute(f"CREATE SCHEMA {SCHEMA}")
        conn.commit()
        for name in INIT_FILES:
            path = INIT / name
            if not path.exists():
                continue
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {SCHEMA}")
                cur.execute(path.read_text(encoding="utf-8"))
            conn.commit()
        _apply_migration(conn)
        yield conn
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.commit()
        conn.close()


@pytest.fixture()
def cur(db):
    """A cursor whose work is rolled back, so cases cannot leak into each other."""
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        c.execute(f"SET search_path TO {SCHEMA}")
        yield c
    db.rollback()


def _a_loan(c):
    """A throwaway boarded loan with a balances row."""
    c.execute(
        "INSERT INTO loans (applicant_name, principal, note_rate_pct, term_months, "
        "                   regular_payment, regular_payment_count, final_payment, "
        "                   schedule_version, status) "
        "VALUES ('Installment Fixture', 15000.00, 7.99, 36, 469.98, 35, 469.87, "
        "        'B1', 'current') RETURNING id")
    loan_id = c.fetchone()["id"]
    c.execute(
        "INSERT INTO balances (loan_id, balance, past_due) VALUES (%s, 15000.00, 0.00)",
        (loan_id,))
    return loan_id


def _assess(c, loan_id, installment_no, amount="35.00"):
    c.execute(
        "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, "
        "                            reason, installment_no) "
        "VALUES (%s, 'fees', %s, 'fee_assessed', 'late fee', %s)",
        (loan_id, amount, installment_no))


# --------------------------------------------------------------------------
# The migration itself.
# --------------------------------------------------------------------------

def test_the_migration_backfills_nothing():
    """0046 must not write a period onto any row that predates it.

    Asserted against the MIGRATION rather than against live data, deliberately.
    An earlier version of this test counted non-null rows in the database, which
    was wrong twice over: any case in this suite that writes an installment made
    it fail, and a real backfill on a fresh volume would have made it pass. What
    "history stays unknown" actually constrains is the migration, so that is what
    is read.

    Those rows were written by a rule with no concept of an installment. There is
    no true value to write, and reconstructing one would mean running an
    allocation policy that did not exist at the time and recording the result as
    though it had been observed (`docs/DEBT.md` D23).
    """
    sql = MIGRATION.read_text(encoding="utf-8")
    statements = " ".join(
        line for line in sql.splitlines() if not line.strip().startswith("--")
    ).upper()
    assert "UPDATE LEDGER_ENTRIES" not in statements, (
        "0046 updates existing ledger rows -- installment_no must stay NULL for "
        "every row written before the column existed")
    assert "SET INSTALLMENT_NO" not in statements
    assert "DEFAULT" not in statements, (
        "a column default would give every future writer an installment it did "
        "not choose")


def test_the_migration_is_re_runnable(db):
    """Applying 0046 twice is not an error.

    `db/init/001_schema.sql` already carries the column and both CHECKs, because
    this repository backports every migration so a fresh volume never needs the
    ALTER path. So the fixture has ALREADY applied 0046 once on top of a schema
    that had it -- and doing it again must still succeed, or the two definitions
    have drifted apart.
    """
    _apply_migration(db)
    _apply_migration(db)


def test_the_column_is_nullable_so_history_stays_unknown(cur):
    """NULL means "never captured", not "installment 0" and not "probably current"."""
    cur.execute(
        "SELECT is_nullable, column_default FROM information_schema.columns "
        "WHERE table_schema = %s AND table_name = 'ledger_entries' "
        "  AND column_name = 'installment_no'", (SCHEMA,))
    row = cur.fetchone()
    assert row is not None, "0046 did not add installment_no"
    assert row["is_nullable"] == "YES"
    assert row["column_default"] is None, (
        "a default would invent an installment for every writer")


def test_the_seeded_ledger_carries_no_installment(cur):
    """On a fresh schema, every pre-existing entry is explicitly unknown.

    `db/init` seeds a ledger (opening balances, and whatever 002/003 write). None
    of it was assessed against an installment, and none of it may claim to have
    been.
    """
    cur.execute("SELECT count(*) AS n FROM ledger_entries "
                "WHERE installment_no IS NOT NULL")
    assert cur.fetchone()["n"] == 0


def test_installment_numbers_are_one_based(cur):
    loan_id = _a_loan(cur)
    for bad in (0, -1):
        cur.execute("SAVEPOINT s")
        with pytest.raises(psycopg2.errors.CheckViolation):
            _assess(cur, loan_id, bad)
        cur.execute("ROLLBACK TO SAVEPOINT s")


def test_only_a_fee_assessment_may_name_an_installment(cur):
    """A `payment` row must not carry one, because payment attribution does not exist.

    Leaving the column open to payments would let a reader assume attribution had
    been captured somewhere. It has not, and the constraint is what stops that
    assumption being available.
    """
    loan_id = _a_loan(cur)
    cur.execute(
        "INSERT INTO payments (loan_id, amount, method) "
        "VALUES (%s, 100.00, 'card') RETURNING id", (loan_id,))
    payment_id = cur.fetchone()["id"]
    cur.execute("SAVEPOINT p")
    with pytest.raises(psycopg2.errors.CheckViolation):
        cur.execute(
            "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, "
            "                            payment_id, installment_no) "
            "VALUES (%s, 'principal', -100.00, 'payment', %s, 3)",
            (loan_id, payment_id))
    cur.execute("ROLLBACK TO SAVEPOINT p")


# --------------------------------------------------------------------------
# One fee per installment.
# --------------------------------------------------------------------------

def test_a_second_fee_for_the_same_installment_is_refused(cur):
    """The rule's central guarantee, enforced where it cannot be bypassed."""
    loan_id = _a_loan(cur)
    _assess(cur, loan_id, 3)
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.UniqueViolation):
        _assess(cur, loan_id, 3, amount="10.00")
    cur.execute("ROLLBACK TO SAVEPOINT s")


def test_a_later_installment_may_take_its_own_fee(cur):
    """"A later scheduled installment that separately becomes overdue may receive
    one fee of its own" -- the client's rule, 2026-08-29."""
    loan_id = _a_loan(cur)
    _assess(cur, loan_id, 3)
    _assess(cur, loan_id, 4)
    cur.execute(
        "SELECT count(*) AS n FROM ledger_entries WHERE loan_id = %s "
        "AND entry_type = 'fee_assessed' AND installment_no IS NOT NULL", (loan_id,))
    assert cur.fetchone()["n"] == 2


def test_the_same_installment_number_on_a_different_loan_is_unaffected(cur):
    """The index is keyed on (loan_id, installment_no), not on the period alone."""
    first = _a_loan(cur)
    second = _a_loan(cur)
    _assess(cur, first, 3)
    _assess(cur, second, 3)


def test_fees_with_no_installment_do_not_collide(cur):
    """A fee that is not installment-scoped writes NULL and is outside the index.

    `policies/fee_schedule.md` prices an NSF charge per returned payment, not per
    period. Two of those on one loan must both be writable -- which is why the
    index is partial on `installment_no IS NOT NULL` rather than on the entry type.
    """
    loan_id = _a_loan(cur)
    cur.execute(
        "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, reason) "
        "VALUES (%s, 'fees', 25.00, 'fee_assessed', 'NSF'), "
        "       (%s, 'fees', 25.00, 'fee_assessed', 'NSF')", (loan_id, loan_id))
    cur.execute(
        "SELECT count(*) AS n FROM ledger_entries WHERE loan_id = %s "
        "AND installment_no IS NULL AND entry_type = 'fee_assessed'", (loan_id,))
    assert cur.fetchone()["n"] == 2


# --------------------------------------------------------------------------
# Concurrency and retry -- the reason this is an index and not an application check.
# --------------------------------------------------------------------------

def test_two_concurrent_assessors_cannot_both_write_one_installment(db):
    """Two sessions, no application lock, one fee.

    The second INSERT blocks on the unique index until the first commits, then
    fails. This is the case an application-level "have we already?" check gets
    wrong: both sessions read "no fee yet" before either writes.

    Opens its own two connections -- one connection cannot demonstrate two
    sessions -- and cleans up after itself, because work that commits is outside
    the rolling-back `cur` fixture.
    """
    a = _connect()
    b = _connect()
    loan_id = None
    try:
        with a.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as ca:
            ca.execute(f"SET search_path TO {SCHEMA}")
            loan_id = _a_loan(ca)
        a.commit()

        with a.cursor() as ca:
            ca.execute(f"SET search_path TO {SCHEMA}")
            _assess(ca, loan_id, 7)
        a.commit()  # a wins

        with b.cursor() as cb:
            cb.execute(f"SET search_path TO {SCHEMA}")
            cb.execute("SET LOCAL lock_timeout = '5s'")
            with pytest.raises(psycopg2.errors.UniqueViolation):
                _assess(cb, loan_id, 7)
        b.rollback()

        with a.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as ca:
            ca.execute(f"SET search_path TO {SCHEMA}")
            ca.execute(
                "SELECT count(*) AS n FROM ledger_entries WHERE loan_id = %s "
                "AND entry_type = 'fee_assessed' AND installment_no = 7", (loan_id,))
            assert ca.fetchone()["n"] == 1
    finally:
        # The ledger is append-only by trigger, so the fixture rows are removed
        # with row-level triggers disabled -- the same escape the other db/tests
        # fixtures use for throwaway data.
        if loan_id is not None:
            with a.cursor() as ca:
                ca.execute(f"SET search_path TO {SCHEMA}")
                ca.execute("SET session_replication_role = replica")
                ca.execute("DELETE FROM ledger_entries WHERE loan_id = %s", (loan_id,))
                ca.execute("DELETE FROM balances WHERE loan_id = %s", (loan_id,))
                ca.execute("DELETE FROM loans WHERE id = %s", (loan_id,))
                ca.execute("SET session_replication_role = origin")
            a.commit()
        a.close()
        b.close()


def test_a_retry_of_the_same_assessment_is_refused_every_time(cur):
    """Idempotency for free: each retry hits the index the first write took."""
    loan_id = _a_loan(cur)
    _assess(cur, loan_id, 12)
    for _ in range(3):
        cur.execute("SAVEPOINT r")
        with pytest.raises(psycopg2.errors.UniqueViolation):
            _assess(cur, loan_id, 12)
        cur.execute("ROLLBACK TO SAVEPOINT r")
    cur.execute(
        "SELECT count(*) AS n FROM ledger_entries WHERE loan_id = %s "
        "AND entry_type = 'fee_assessed' AND installment_no = 12", (loan_id,))
    assert cur.fetchone()["n"] == 1
