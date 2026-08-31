"""db/migrations/0046 -- one late fee per installment, enforced by the database.

`docs/DEBT.md` D23 names this as the part of the fix that has to live in the
schema rather than in application code: a unique index makes the concurrent
assessor and the retry safe *by construction*, where an application check makes
them safe only while every writer remembers to take the same lock.

These cases run against real PostgreSQL because that is the only place the
constraint exists. A fake would prove the test, not the index.
"""
import os
import pathlib

import psycopg2
import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

DATABASE_URL = os.getenv("DATABASE_URL")


@pytest.fixture()
def conn():
    """A rolled-back connection, or a skip when there is no database.

    The guard is on the FIXTURE rather than on the module, so the one case here
    that reads the migration text instead of the database still runs without a
    Postgres. A module-level `pytestmark` skipped it too, which meant the
    no-backfill guarantee -- the part that needs no database at all -- was silently
    unchecked on any run without `DATABASE_URL`.
    """
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set -- no Postgres to test against")
    c = psycopg2.connect(DATABASE_URL)
    c.autocommit = False
    yield c
    c.rollback()
    c.close()


def _a_loan(cur):
    """A throwaway boarded loan with a balances row, rolled back by the fixture."""
    cur.execute(
        "INSERT INTO loans (applicant_name, principal, note_rate_pct, term_months, "
        "                   regular_payment, regular_payment_count, final_payment, "
        "                   schedule_version, status) "
        "VALUES ('Installment Fixture', 15000.00, 7.99, 36, 469.98, 35, 469.87, "
        "        'B1', 'current') RETURNING id")
    loan_id = cur.fetchone()[0]
    cur.execute(
        "INSERT INTO balances (loan_id, balance, past_due) VALUES (%s, 15000.00, 0.00)",
        (loan_id,))
    return loan_id


def _assess(cur, loan_id, installment_no, amount="35.00"):
    cur.execute(
        "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, "
        "                            reason, installment_no) "
        "VALUES (%s, 'fees', %s, 'fee_assessed', 'late fee', %s)",
        (loan_id, amount, installment_no))


# --------------------------------------------------------------------------
# The column exists and says what it means.
# --------------------------------------------------------------------------

def test_the_column_is_nullable_so_history_stays_unknown(conn):
    """Existing rows carry NULL, and NULL means "never captured".

    No backfill is possible: those entries were written by a rule with no concept
    of an installment, so there is no true value to write. This asserts the schema
    permits that state rather than demanding a fabricated number.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT is_nullable, column_default FROM information_schema.columns "
            "WHERE table_name = 'ledger_entries' AND column_name = 'installment_no'")
        row = cur.fetchone()
    assert row is not None, "0046 did not add installment_no"
    assert row[0] == "YES"
    assert row[1] is None, "a default would invent an installment for every writer"


def test_the_migration_backfills_nothing():
    """0046 must not write a period onto any row that predates it.

    Asserted against the MIGRATION rather than against live data, deliberately.
    An earlier version of this test counted non-null rows in the database, which
    was wrong twice over: any test in this suite that writes an installment makes
    it fail, and a real backfill on a fresh volume would make it pass. What
    "history stays unknown" actually constrains is the migration, so that is what
    is read.

    Those rows were written by a rule with no concept of an installment. There is
    no true value to write, and reconstructing one would mean running an
    allocation policy that did not exist at the time and recording the result as
    though it had been observed (`docs/DEBT.md` D23).
    """
    sql = (REPO / "db" / "migrations" / "0046_ledger_installment_no.sql").read_text(
        encoding="utf-8")
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


def test_installment_numbers_are_one_based(conn):
    with conn.cursor() as cur:
        loan_id = _a_loan(cur)
        for bad in (0, -1):
            with pytest.raises(psycopg2.errors.CheckViolation):
                with conn.cursor() as inner:
                    inner.execute("SAVEPOINT s")
                    _assess(inner, loan_id, bad)
            conn.rollback()
            cur.execute("SELECT 1")  # connection still usable


def test_only_a_fee_assessment_may_name_an_installment(conn):
    """A `payment` row must not carry one, because payment attribution does not exist.

    Leaving the column open to payments would let a reader assume attribution had
    been captured somewhere. It has not, and the constraint is what stops that
    assumption being available.
    """
    with conn.cursor() as cur:
        loan_id = _a_loan(cur)
        cur.execute(
            "INSERT INTO payments (loan_id, amount, method, auth_status, idempotency_key) "
            "VALUES (%s, 100.00, 'card', 'captured', %s) RETURNING id",
            (loan_id, f"idem-installment-{loan_id}"))
        payment_id = cur.fetchone()[0]
        with pytest.raises(psycopg2.errors.CheckViolation):
            cur.execute(
                "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, "
                "                            payment_id, installment_no) "
                "VALUES (%s, 'principal', -100.00, 'payment', %s, 3)",
                (loan_id, payment_id))
    conn.rollback()


# --------------------------------------------------------------------------
# One fee per installment.
# --------------------------------------------------------------------------

def test_a_second_fee_for_the_same_installment_is_refused(conn):
    """The rule's central guarantee, enforced where it cannot be bypassed."""
    with conn.cursor() as cur:
        loan_id = _a_loan(cur)
        _assess(cur, loan_id, 3)
        with pytest.raises(psycopg2.errors.UniqueViolation):
            _assess(cur, loan_id, 3, amount="10.00")
    conn.rollback()


def test_a_later_installment_may_take_its_own_fee(conn):
    """"A later scheduled installment that separately becomes overdue may receive
    one fee of its own" -- the client's rule, 2026-08-29."""
    with conn.cursor() as cur:
        loan_id = _a_loan(cur)
        _assess(cur, loan_id, 3)
        _assess(cur, loan_id, 4)
        cur.execute(
            "SELECT count(*) FROM ledger_entries WHERE loan_id = %s "
            "AND entry_type = 'fee_assessed' AND installment_no IS NOT NULL",
            (loan_id,))
        assert cur.fetchone()[0] == 2
    conn.rollback()


def test_the_same_installment_number_on_a_different_loan_is_unaffected(conn):
    """The index is keyed on (loan_id, installment_no), not on the period alone."""
    with conn.cursor() as cur:
        first = _a_loan(cur)
        second = _a_loan(cur)
        _assess(cur, first, 3)
        _assess(cur, second, 3)
    conn.rollback()


def test_fees_with_no_installment_do_not_collide(conn):
    """A fee that is not installment-scoped writes NULL and is outside the index.

    `policies/fee_schedule.md` prices an NSF charge per returned payment, not per
    period. Two of those on one loan must both be writable -- which is why the
    index is partial on `installment_no IS NOT NULL` rather than on the entry type.
    """
    with conn.cursor() as cur:
        loan_id = _a_loan(cur)
        cur.execute(
            "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, reason) "
            "VALUES (%s, 'fees', 25.00, 'fee_assessed', 'NSF'), "
            "       (%s, 'fees', 25.00, 'fee_assessed', 'NSF')",
            (loan_id, loan_id))
        cur.execute(
            "SELECT count(*) FROM ledger_entries WHERE loan_id = %s "
            "AND installment_no IS NULL AND entry_type = 'fee_assessed'", (loan_id,))
        assert cur.fetchone()[0] == 2
    conn.rollback()


# --------------------------------------------------------------------------
# Concurrency and retry -- the reason this is an index and not an application check.
# --------------------------------------------------------------------------

def test_two_concurrent_assessors_cannot_both_write_one_installment():
    """Two sessions, no application lock, one fee.

    The second INSERT blocks on the unique index until the first commits, then
    fails. This is the case an application-level "have we already?" check gets
    wrong: both sessions read "no fee yet" before either writes.

    Opens its own two connections rather than taking the `conn` fixture -- one
    connection cannot demonstrate two sessions -- so it carries its own skip.
    """
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set -- no Postgres to test against")
    a = psycopg2.connect(DATABASE_URL)
    b = psycopg2.connect(DATABASE_URL)
    a.autocommit = False
    b.autocommit = False
    try:
        with a.cursor() as cur_a:
            loan_id = _a_loan(cur_a)
        a.commit()

        with a.cursor() as cur_a:
            _assess(cur_a, loan_id, 7)
        # b tries the same installment before a has committed.
        with b.cursor() as cur_b:
            cur_b.execute("SET LOCAL lock_timeout = '5s'")
            a.commit()  # a wins
            with pytest.raises(psycopg2.errors.UniqueViolation):
                _assess(cur_b, loan_id, 7)
        b.rollback()

        with a.cursor() as cur_a:
            cur_a.execute(
                "SELECT count(*) FROM ledger_entries WHERE loan_id = %s "
                "AND entry_type = 'fee_assessed' AND installment_no = 7", (loan_id,))
            assert cur_a.fetchone()[0] == 1
            # Clean up: the ledger is append-only, so remove the fixture loan's
            # rows with the trigger disabled the way other db/tests fixtures do.
            cur_a.execute("SET session_replication_role = replica")
            cur_a.execute("DELETE FROM ledger_entries WHERE loan_id = %s", (loan_id,))
            cur_a.execute("DELETE FROM balances WHERE loan_id = %s", (loan_id,))
            cur_a.execute("DELETE FROM loans WHERE id = %s", (loan_id,))
            cur_a.execute("SET session_replication_role = origin")
        a.commit()
    finally:
        a.close()
        b.close()


def test_a_retry_of_the_same_assessment_is_refused_not_duplicated(conn):
    """Idempotency for free: the retry hits the same index the first write took."""
    with conn.cursor() as cur:
        loan_id = _a_loan(cur)
        _assess(cur, loan_id, 12)
        for _ in range(3):
            with pytest.raises(psycopg2.errors.UniqueViolation):
                with conn.cursor() as inner:
                    inner.execute("SAVEPOINT r")
                    _assess(inner, loan_id, 12)
            conn.rollback()
            # Re-establish the fixture rows the rollback removed.
            loan_id = _a_loan(cur)
            _assess(cur, loan_id, 12)
    conn.rollback()
