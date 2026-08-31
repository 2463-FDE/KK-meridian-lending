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
import threading

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


def _a_scheduleless_loan(c):
    """A legacy loan: a term, and NO stored contractual schedule.

    `loans_schedule_all_or_nothing` permits this -- the four schedule columns are
    all-or-nothing and all-NULL is legal, which is what a loan boarded before
    `db/migrations/0030` looks like. `installments_for()` refuses to expand it.
    """
    c.execute(
        "INSERT INTO loans (applicant_name, principal, note_rate_pct, term_months, "
        "                   status) "
        "VALUES ('Scheduleless Legacy', 5000.00, 7.99, 24, 'current') RETURNING id")
    loan_id = c.fetchone()["id"]
    c.execute(
        "INSERT INTO balances (loan_id, balance, past_due) VALUES (%s, 5000.00, 0.00)",
        (loan_id,))
    return loan_id


@pytest.mark.parametrize("n", [1, 12, 24])
def test_a_loan_with_no_stored_schedule_cannot_have_an_installment_fee(cur, n):
    """Codex review SCHEDULELESS-INSTALLMENT-002.

    Checking `term_months` alone was not enough. A legacy loan can carry a term
    and no schedule, so the database would have accepted
    `fee_assessed + installment_no = 1` for a loan whose servicing layer refuses
    to derive any installment at all -- a ledger row whose claimed identity
    nothing can resolve.

    Every value here is inside the 24-month term, so the term check would pass.
    What refuses them is the absence of the contract.
    """
    loan_id = _a_scheduleless_loan(cur)
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _assess(cur, loan_id, n)
    cur.execute("ROLLBACK TO SAVEPOINT s")
    assert "no stored contractual schedule" in str(exc.value)


def test_a_scheduleless_loan_may_still_take_a_fee_with_no_installment(cur):
    """The refusal is about the CITATION, not about the loan.

    A legacy loan can still be charged a fee -- that is what the superseded
    arrears rule does today. What it cannot do is claim a period it has no
    schedule for. Without this case, a trigger that refused every fee on a
    scheduleless loan would look correct.
    """
    loan_id = _a_scheduleless_loan(cur)
    cur.execute(
        "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, reason) "
        "VALUES (%s, 'fees', 35.00, 'fee_assessed', 'arrears rule, no period')",
        (loan_id,))
    # Scoped to the fee. Inserting the `balances` row fires
    # `balances_capture_legacy_delta`, which writes its own NULL-installment
    # `legacy_direct_write` entry -- counting every NULL row here would have been
    # counting that too.
    cur.execute(
        "SELECT count(*) AS n FROM ledger_entries WHERE loan_id = %s "
        "AND entry_type = 'fee_assessed' AND installment_no IS NULL", (loan_id,))
    assert cur.fetchone()["n"] == 1


def test_the_database_and_the_servicing_layer_agree_on_derivability(cur):
    """The invariant the two halves of this PR share.

    `installments_for()` raises `ScheduleNotAvailable` exactly when the four
    schedule columns are absent; the trigger refuses an installment citation in
    exactly the same case. If the database accepted a period the application
    cannot derive, the fee would be a false identity; if the application derived
    one the database rejects, the fee could not be written at all.

    Asserted by exercising both sides against the same two rows rather than by
    reading either implementation.
    """
    scheduled = _a_loan(cur)
    scheduleless = _a_scheduleless_loan(cur)

    # Database side.
    _assess(cur, scheduled, 5)
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.RaiseException):
        _assess(cur, scheduleless, 5)
    cur.execute("ROLLBACK TO SAVEPOINT s")

    # Application side, on rows shaped like the ones just written.
    import sys
    sys.path.insert(0, str(REPO / "services" / "servicing-service"))
    try:
        from app import installments as inst
    except Exception:  # pragma: no cover - servicing deps absent on this runner
        pytest.skip("servicing-service app not importable from db/tests")
    finally:
        sys.path.pop(0)

    cur.execute(
        "SELECT principal, note_rate_pct, term_months, regular_payment, "
        "       final_payment, schedule_version, opened_at "
        "  FROM loans WHERE id IN (%s, %s) ORDER BY id", (scheduled, scheduleless))
    rows = cur.fetchall()
    ok, legacy = (dict(rows[0]), dict(rows[1])) if rows[0]["schedule_version"] \
        else (dict(rows[1]), dict(rows[0]))

    assert inst.installments_for(ok), "a scheduled loan must expand"
    with pytest.raises(inst.ScheduleNotAvailable):
        inst.installments_for(legacy)


@pytest.mark.parametrize("beyond", [37, 48, 999])
def test_an_installment_past_the_end_of_the_term_is_refused(cur, beyond):
    """Codex review FEE-INSTALLMENT-BOUNDS-001.

    With only `installment_no >= 1`, a 36-month loan accepted `installment_no =
    37` or 999, and the unique index then guaranteed "one fee per installment
    NUMBER" rather than one fee per real missed installment. A fee could claim an
    identity that was false while satisfying every constraint.

    The fixture loan has `term_months = 36`, so each of these is past the end of
    its schedule. Enforced by a trigger because a CHECK may not read `loans`.
    """
    loan_id = _a_loan(cur)
    cur.execute("SAVEPOINT b")
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _assess(cur, loan_id, beyond)
    cur.execute("ROLLBACK TO SAVEPOINT b")
    assert "past the end" in str(exc.value)
    assert "36-month term" in str(exc.value), (
        "the refusal should name the term it was measured against")


def test_the_last_installment_of_the_term_is_allowed(cur):
    """The boundary is inclusive: installment 36 of a 36-month loan is real.

    Asserted separately from the refusals above because an off-by-one in the
    trigger would reject the final installment -- the one most likely to carry a
    fee on a loan that ran to term -- and every out-of-range case would still pass.
    """
    loan_id = _a_loan(cur)
    _assess(cur, loan_id, 36)
    cur.execute(
        "SELECT count(*) AS n FROM ledger_entries WHERE loan_id = %s "
        "AND installment_no = 36", (loan_id,))
    assert cur.fetchone()["n"] == 1


def test_a_null_installment_is_not_validated_against_the_term(cur):
    """The trigger must not turn every other ledger writer into a schedule check.

    A payment, an adjustment, an opening balance: none carries an installment, and
    the trigger returns early for them. Without this case a trigger that raised on
    NULL would break the entire money path and only be caught elsewhere.
    """
    loan_id = _a_loan(cur)
    cur.execute(
        "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, reason) "
        "VALUES (%s, 'fees', 25.00, 'fee_assessed', 'NSF, no period')", (loan_id,))
    cur.execute(
        "SELECT count(*) AS n FROM ledger_entries WHERE loan_id = %s "
        "AND installment_no IS NULL", (loan_id,))
    assert cur.fetchone()["n"] >= 1


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
    """Two sessions genuinely overlapping, one fee.

    Codex review TEST-CONC-001: the first version of this case committed session A
    *before* session B inserted, which made it a sequential unique-violation test
    wearing a concurrency name. It would have passed against an application-level
    "have we already?" check -- the exact defect the index exists to rule out --
    because B's read would have happened after A's commit.

    So B now inserts while A is still UNCOMMITTED, from a worker thread. Postgres
    makes B's insert block on the uncommitted unique-index entry rather than fail;
    the test asserts that it is still blocked (the thread has not finished) before
    A commits, and only then does B resolve into a `UniqueViolation`. That ordering
    is what distinguishes "the database serialised two overlapping writers" from
    "the second writer looked after the first had finished".

    Opens its own two connections -- one cannot demonstrate two sessions -- and
    cleans up after itself, because work that commits is outside the rolling-back
    `cur` fixture.
    """
    a = _connect()
    b = _connect()
    loan_id = None
    outcome: dict = {}

    def _b_inserts():
        """Runs in a worker thread; blocks inside the INSERT until A commits."""
        try:
            with b.cursor() as cb:
                cb.execute(f"SET search_path TO {SCHEMA}")
                # Generous: this MUST block until A commits, and a short timeout
                # would turn the property under test into a flaky failure.
                cb.execute("SET LOCAL lock_timeout = '30s'")
                _assess(cb, loan_id, 7)
            outcome["result"] = "inserted"
        except psycopg2.errors.UniqueViolation:
            outcome["result"] = "unique_violation"
        except Exception as exc:  # noqa: BLE001 - reported, not swallowed
            outcome["result"] = f"unexpected: {type(exc).__name__}: {exc}"

    try:
        with a.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as ca:
            ca.execute(f"SET search_path TO {SCHEMA}")
            loan_id = _a_loan(ca)
        a.commit()

        # A writes and does NOT commit.
        with a.cursor() as ca:
            ca.execute(f"SET search_path TO {SCHEMA}")
            _assess(ca, loan_id, 7)

        worker = threading.Thread(target=_b_inserts, daemon=True)
        worker.start()

        # B must still be inside its INSERT, waiting on A's uncommitted index
        # entry. If it has already finished here, the two writers did not overlap
        # and this case is not testing what it claims.
        worker.join(timeout=3.0)
        assert worker.is_alive(), (
            "session B finished before A committed, so the two writers never "
            "overlapped -- this case would pass against an application-level "
            "check and is not testing the index"
        )

        a.commit()  # A wins; B's wait now resolves

        worker.join(timeout=30.0)
        assert not worker.is_alive(), "session B never resolved after A committed"
        assert outcome["result"] == "unique_violation", (
            f"expected B to lose on the unique index; got {outcome['result']!r}")

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


# --------------------------------------------------------------------------
# CONTRACT INTEGRITY.
#
# The schedule trigger proves installment N is real AT INSERT TIME. `loans` has
# no immutability trigger of its own and the schedule columns are plain updatable
# columns, so that is only half a guarantee:
#
#   A. TOCTOU        -- the contract changes between the check and the commit;
#   B. after the fact -- the contract changes after the fee is on the ledger,
#                        which is append-only, so the entry cannot be corrected.
#
# Invariant: once an installment-scoped ledger fee cites installment N, the stored
# contractual facts that make N a real installment cannot silently change
# underneath that immutable entry.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("column,value", [
    ("term_months", "24"),
    ("schedule_version", "NULL"),
    ("regular_payment", "500.00"),
    ("regular_payment_count", "23"),
    ("final_payment", "501.00"),
    ("note_rate_pct", "9.990"),
    ("opened_at", "now() - interval '400 days'"),
])
def test_a_cited_contract_cannot_be_changed(cur, column, value):
    """Every field installment identity is derived from.

    `note_rate_pct` and `opened_at` are in the list because `installments.py`
    anchors due dates on `opened_at` and splits interest at the note rate -- change
    either and the period a fee refers to quietly becomes a different one, even
    though the term is untouched.
    """
    loan_id = _a_loan(cur)
    _assess(cur, loan_id, 5)
    cur.execute("SAVEPOINT s")
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        cur.execute(f"UPDATE loans SET {column} = {value} WHERE id = %s", (loan_id,))
    cur.execute("ROLLBACK TO SAVEPOINT s")
    assert "citing an installment" in str(exc.value)


def test_an_uncited_contract_may_still_be_corrected(cur):
    """Scoped to CITED loans, and this is what that scoping protects.

    `db/init/003_seed_bulk.sql` back-fills exactly these columns from the accepted
    offer, and correcting a contract on a loan nothing has cited is a different
    question this change has no authority to answer. Freezing the whole table would
    have broken seeding and decided a policy nobody asked for.
    """
    loan_id = _a_loan(cur)
    cur.execute("UPDATE loans SET term_months = 24, regular_payment_count = 23 "
                " WHERE id = %s", (loan_id,))
    cur.execute("SELECT term_months FROM loans WHERE id = %s", (loan_id,))
    assert cur.fetchone()["term_months"] == 24


def test_a_change_that_does_not_touch_the_schedule_is_unaffected(cur):
    """The guard must not freeze the whole row.

    `status` moves through the servicing lifecycle on loans that certainly do carry
    fees. A guard that blocked every UPDATE would break delinquency handling while
    looking like it only protected the schedule.
    """
    loan_id = _a_loan(cur)
    _assess(cur, loan_id, 6)
    cur.execute("UPDATE loans SET status = 'delinquent' WHERE id = %s", (loan_id,))
    cur.execute("SELECT status FROM loans WHERE id = %s", (loan_id,))
    assert cur.fetchone()["status"] == "delinquent"


def test_a_fee_with_no_installment_does_not_freeze_the_contract(cur):
    """Only an installment-scoped citation creates the dependency.

    A legacy loan charged under the superseded arrears rule writes
    `installment_no = NULL`, which asserts nothing about the schedule -- so it must
    not freeze it.
    """
    loan_id = _a_loan(cur)
    cur.execute(
        "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, reason) "
        "VALUES (%s, 'fees', 35.00, 'fee_assessed', 'arrears rule, no period')",
        (loan_id,))
    cur.execute("UPDATE loans SET term_months = 24, regular_payment_count = 23 "
                " WHERE id = %s", (loan_id,))
    cur.execute("SELECT term_months FROM loans WHERE id = %s", (loan_id,))
    assert cur.fetchone()["term_months"] == 24


def test_the_contract_cannot_change_under_an_in_flight_fee(db):
    """The TOCTOU half, with two genuinely overlapping sessions.

    T1 writes the fee and does NOT commit. T2 tries to shorten the term. It must
    BLOCK on the loan row T1 holds -- asserted by the thread still being alive --
    rather than slipping in before the ledger entry lands. Once T1 commits, the
    freeze trigger sees the citation and refuses T2 outright.

    Without the `FOR SHARE`, T2 would read a loan nothing had cited yet, pass the
    freeze check, and commit a shorter term while T1's fee for installment 30 was
    still in flight.
    """
    if not DATABASE_URL:
        pytest.skip("DATABASE_URL not set -- no Postgres to test against")
    setup = _connect()
    with setup.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        c.execute("SET search_path TO " + SCHEMA)
        loan_id = _a_loan(c)
    setup.commit()

    t1 = _connect()
    outcome = {}

    def _shorten():
        c2 = _connect()
        try:
            with c2.cursor() as cur2:
                cur2.execute("SET search_path TO " + SCHEMA)
                cur2.execute("SET LOCAL lock_timeout = '30s'")
                cur2.execute("UPDATE loans SET term_months = 24, "
                             "regular_payment_count = 23 WHERE id = %s", (loan_id,))
            c2.commit()
            outcome["state"] = "committed"
        except Exception as exc:                       # noqa: BLE001 - reported
            outcome["state"] = type(exc).__name__
            c2.rollback()
        finally:
            c2.close()

    try:
        with t1.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c1:
            c1.execute("SET search_path TO " + SCHEMA)
            _assess(c1, loan_id, 30)          # installment 30 of 36; NOT committed

        worker = threading.Thread(target=_shorten, daemon=True)
        worker.start()
        worker.join(timeout=3.0)
        assert worker.is_alive(), (
            "the term was shortened while the fee was still in flight -- the "
            "installment trigger is not holding the loan row, so a fee can be "
            "written for a period the contract no longer has")

        t1.commit()
        worker.join(timeout=30.0)
        assert outcome.get("state") == "RaiseException", (
            f"expected the contract change to be refused once the fee committed; "
            f"got {outcome.get('state')!r}")

        with t1.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c1:
            c1.execute("SET search_path TO " + SCHEMA)
            c1.execute("SELECT term_months FROM loans WHERE id = %s", (loan_id,))
            assert c1.fetchone()["term_months"] == 36, "the term changed after all"
    finally:
        t1.rollback()
        t1.close()
        setup.close()
