"""ADR 0010 step 2: the ledger, its projection, and the back-fill that seeds it.

Against real PostgreSQL, in a throwaway schema built from `db/init` plus the
migration, because every invariant here is enforced by the database and none of
them can be tested against a mock: a trigger that does not fire, a CHECK that
does not hold and a unique index that does not exist all look identical from
Python.

What this migration is responsible for -- the binding invariants from ADR 0010:

  1. entries are immutable
  2. an entry is a signed delta, never a total
  3. projection writes `balances`; transitional direct writers are delta-captured
  4. the sign is keyed to what the borrower owes
  5. a human-directed entry names the human
  7. exactly one entry per (payment_id, component)

and the back-fill: the projection must equal the ledger sum for every loan, and
running the migration twice must not double anything.
"""
import os
import pathlib
import threading
import time
import uuid

import psycopg2
import psycopg2.extras
import pytest

DATABASE_URL = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against"
)

REPO = pathlib.Path(__file__).resolve().parents[2]
SCHEMA = "ledger_projection_test"
INIT = REPO / "db" / "init"
MIGRATION = REPO / "db" / "migrations" / "0035_ledger_entries.sql"
INIT_FILES = ("001_schema.sql", "002_seed.sql", "003_seed_bulk.sql",
              "004_decision_events.sql", "005_manual_reviews.sql",
              "006_decision_attempts.sql",
              "007_ledger_opening_balances.sql")


def _exec(conn, sql, params=None):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(sql, params or ())
        return cur.fetchall() if cur.description else []


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


@pytest.fixture(scope="module")
def db():
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


def _a_loan(conn):
    return _exec(conn, "SELECT loan_id FROM balances ORDER BY loan_id LIMIT 1")[0]["loan_id"]


def _a_payment(conn, loan, amount="50.00"):
    """A payment on `loan`, because a 'payment' entry must now name one.

    Two constraints make this necessary rather than tidy:
    `ledger_payment_provenance` requires a payment entry to carry a payment_id
    (and forbids one on every other type), and `ledger_entries_payment_loan_fk`
    requires that payment to belong to the same loan the entry moves.
    """
    return _exec(conn, "INSERT INTO payments (loan_id, amount, method) "
                       "VALUES (%s, %s, 'card') RETURNING id",
                 (loan, amount))[0]["id"]


# --- the back-fill ----------------------------------------------------------

def test_the_projection_equals_the_ledger_sum_for_every_loan(db):
    """Per loan, never in aggregate.

    A global sum reports "in balance" while two loans are wrong by equal and
    opposite amounts -- the check that passes while two borrowers are wrong.
    """
    breaks = _exec(db, """
        SELECT b.loan_id, b.balance,
               COALESCE((SELECT SUM(amount) FROM ledger_entries le
                          WHERE le.loan_id = b.loan_id AND le.component = 'principal'), 0) AS ledger
          FROM balances b
    """)
    wrong = [r for r in breaks if r["balance"] != r["ledger"]]
    assert not wrong, f"projection disagrees with the ledger for {wrong[:5]}"


def test_past_due_is_projected_too(db):
    wrong = _exec(db, """
        SELECT b.loan_id FROM balances b
         WHERE COALESCE(b.past_due, 0) <> COALESCE(
               (SELECT SUM(amount) FROM ledger_entries le
                 WHERE le.loan_id = b.loan_id AND le.component = 'fees'), 0)
    """)
    assert not wrong, f"past_due disagrees with the fees component for {wrong[:5]}"


def test_the_backfill_seeded_something(db):
    """Seeded state has one canonical meaning on fresh and migrated databases."""
    n = _exec(db, "SELECT count(*) AS n FROM ledger_entries "
                  "WHERE entry_type = 'opening_balance'")[0]["n"]
    assert n > 0, "ledger initialization produced no opening entries"


def test_a_session_flag_cannot_suppress_a_normal_projection(db):
    loan = _a_loan(db)
    before = _exec(db, "SELECT balance FROM balances WHERE loan_id=%s", (loan,))[0]["balance"]
    _exec(db, "SELECT set_config('meridian.suppress_projection','on',true)")
    _exec(db, "INSERT INTO ledger_entries(loan_id,component,amount,entry_type,actor_id,actor_role) "
              "VALUES(%s,'principal',9,'adjustment',1,'admin')", (loan,))
    after = _exec(db, "SELECT balance FROM balances WHERE loan_id=%s", (loan,))[0]["balance"]
    assert after == before + 9
    db.rollback()


@pytest.mark.parametrize("entry_type", ["opening_balance", "legacy_direct_write"])
def test_system_only_entry_types_cannot_be_inserted_directly(db, entry_type):
    loan = _a_loan(db)
    with pytest.raises(psycopg2.errors.RaiseException):
        _exec(db, "INSERT INTO ledger_entries(loan_id,component,amount,entry_type,reason) "
                  "VALUES(%s,'principal',11,%s,'attempted direct system entry')",
              (loan, entry_type))
    db.rollback()


def test_the_initialization_gate_cannot_be_reopened(db):
    with pytest.raises(psycopg2.errors.RaiseException):
        _exec(db, "UPDATE ledger_control SET initialization_open=TRUE")
    db.rollback()


def test_running_the_migration_again_does_not_double_anything(db):
    """The rollback story depends on this: a second cutover attempt must not
    re-seed, or every loan with an opening balance doubles."""
    before = _exec(db, "SELECT count(*) AS n, COALESCE(SUM(amount),0) AS total "
                       "FROM ledger_entries")[0]
    _apply_migration(db)
    after = _exec(db, "SELECT count(*) AS n, COALESCE(SUM(amount),0) AS total "
                      "FROM ledger_entries")[0]

    assert after["n"] == before["n"], "re-running the migration inserted more entries"
    assert after["total"] == before["total"]


def test_the_backfill_did_not_move_any_balance(db):
    """The projection is suppressed during the back-fill because `balances`
    already holds those amounts. If it were not, every loan would double."""
    wrong = _exec(db, """
        SELECT b.loan_id FROM balances b
         WHERE b.balance <> COALESCE((SELECT SUM(amount) FROM ledger_entries le
                                       WHERE le.loan_id = b.loan_id
                                         AND le.component = 'principal'), 0)
    """)
    assert not wrong


# --- invariant 3: the projection is live ------------------------------------

def test_an_inserted_entry_moves_the_balance(db):
    loan = _a_loan(db)
    before = _exec(db, "SELECT balance FROM balances WHERE loan_id = %s", (loan,))[0]["balance"]

    pay = _a_payment(db, loan, "25.00")
    _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, payment_id) "
              "VALUES (%s, 'principal', -25.00, 'payment', %s)", (loan, pay))
    db.commit()

    after = _exec(db, "SELECT balance FROM balances WHERE loan_id = %s", (loan,))[0]["balance"]
    assert after == before - 25, f"balance did not follow the ledger: {before} -> {after}"


def test_two_entries_compose_rather_than_racing(db):
    """Invariant 2, and the reason this closes D3.

    Two deltas applied by the database sum. Two absolute writes computed from a
    prior read do not -- one overwrites the other, which is the lost update.
    """
    loan = _a_loan(db)
    before = _exec(db, "SELECT balance FROM balances WHERE loan_id = %s", (loan,))[0]["balance"]

    first, second = _a_payment(db, loan, "10.00"), _a_payment(db, loan, "15.00")
    _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, payment_id) "
              "VALUES (%s, 'principal', -10.00, 'payment', %s), "
              "       (%s, 'principal', -15.00, 'payment', %s)",
          (loan, first, loan, second))
    db.commit()

    after = _exec(db, "SELECT balance FROM balances WHERE loan_id = %s", (loan,))[0]["balance"]
    assert after == before - 25, "the two entries did not both apply"


def test_interest_projects_nowhere(db):
    loan = _a_loan(db)
    b = _exec(db, "SELECT balance, past_due FROM balances WHERE loan_id = %s", (loan,))[0]
    pay = _a_payment(db, loan, "5.00")
    _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, payment_id) "
              "VALUES (%s, 'interest', -5.00, 'payment', %s)", (loan, pay))
    db.commit()

    after = _exec(db, "SELECT balance, past_due FROM balances WHERE loan_id = %s", (loan,))[0]
    assert after["balance"] == b["balance"] and after["past_due"] == b["past_due"], (
        "an interest entry moved a balance column -- interest is owed within a "
        "payment, not carried as a separate balance"
    )


# --- invariant 1: immutability ----------------------------------------------

@pytest.mark.parametrize("statement", [
    "UPDATE ledger_entries SET amount = 1 WHERE id = %s",
    "DELETE FROM ledger_entries WHERE id = %s",
])
def test_an_entry_cannot_be_changed_or_removed(db, statement):
    loan = _a_loan(db)
    pay = _a_payment(db, loan, "1.00")
    entry = _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, "
                      "payment_id) "
                      "VALUES (%s, 'principal', -1.00, 'payment', %s) RETURNING id",
                  (loan, pay))[0]["id"]
    db.commit()

    with pytest.raises(psycopg2.errors.RaiseException):
        _exec(db, statement, (entry,))
    db.rollback()


# --- invariant 4: the sign is keyed to the effect ---------------------------

@pytest.mark.parametrize("entry_type, amount", [
    ("payment", 10.00),          # a payment that increases what is owed
    ("fee_assessed", -10.00),    # a fee that reduces it
    ("disbursement", -10.00),
    ("fee_waived", 10.00),
])
def test_a_wrong_signed_entry_is_refused(db, entry_type, amount):
    loan = _a_loan(db)
    with pytest.raises(psycopg2.errors.CheckViolation):
        _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, "
                  "actor_id, actor_role) VALUES (%s, 'principal', %s, %s, 1, 'csr')",
              (loan, amount, entry_type))
    db.rollback()


def test_an_adjustment_may_go_either_way(db):
    """The one type that may, which is exactly why it is the one needing a
    second approver (ADR 0011)."""
    loan = _a_loan(db)
    for amount in (12.00, -12.00):
        _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, "
                  "actor_id, actor_role) VALUES (%s, 'principal', %s, 'adjustment', 1, 'csr')",
              (loan, amount))
    db.commit()


def test_a_zero_entry_is_refused(db):
    """A movement of nothing is not a movement, and it would make the ledger
    unreadable as a history."""
    loan = _a_loan(db)
    with pytest.raises(psycopg2.errors.CheckViolation):
        _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, "
                  "actor_id, actor_role) VALUES (%s, 'principal', 0, 'adjustment', 1, 'csr')",
              (loan,))
    db.rollback()


# --- invariant 5: a human-directed entry names the human --------------------

@pytest.mark.parametrize("entry_type", ["adjustment", "fee_waived"])
def test_a_human_directed_entry_without_an_actor_is_refused(db, entry_type):
    loan = _a_loan(db)
    amount = -5.00 if entry_type == "fee_waived" else 5.00
    with pytest.raises(psycopg2.errors.CheckViolation):
        _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type) "
                  "VALUES (%s, 'fees', %s, %s)", (loan, amount, entry_type))
    db.rollback()


def test_a_machine_originated_entry_needs_no_actor(db):
    """'payment' especially: servicing's apply receives an amount and a
    payment_id and no actor. Requiring one would fail every real payment."""
    loan = _a_loan(db)
    pay = _a_payment(db, loan, "3.00")
    _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, payment_id) "
              "VALUES (%s, 'principal', -3.00, 'payment', %s)", (loan, pay))
    db.commit()


# --- invariant 7: one entry per (payment_id, component) ---------------------

def test_the_same_payment_cannot_post_twice_for_one_component(db):
    loan = _a_loan(db)
    pay = _exec(db, "INSERT INTO payments (loan_id, amount, method) "
                    "VALUES (%s, 50.00, 'card') RETURNING id", (loan,))[0]["id"]
    _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, payment_id) "
              "VALUES (%s, 'principal', -50.00, 'payment', %s)", (loan, pay))
    db.commit()

    with pytest.raises(psycopg2.errors.UniqueViolation):
        _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, payment_id) "
                  "VALUES (%s, 'principal', -50.00, 'payment', %s)", (loan, pay))
    db.rollback()


def test_one_payment_may_span_components(db):
    """Invariant 7 is per PAIR, not per payment. A waterfall (D14) splits one
    payment across fees, interest and principal by definition -- a per-payment
    rule would forbid the thing the ledger exists to allow."""
    loan = _a_loan(db)
    pay = _exec(db, "INSERT INTO payments (loan_id, amount, method) "
                    "VALUES (%s, 75.00, 'card') RETURNING id", (loan,))[0]["id"]

    _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, payment_id) "
              "VALUES (%s, 'fees', -25.00, 'payment', %s), "
              "       (%s, 'principal', -50.00, 'payment', %s)", (loan, pay, loan, pay))
    db.commit()

    n = _exec(db, "SELECT count(*) AS n FROM ledger_entries WHERE payment_id = %s",
              (pay,))[0]["n"]
    assert n == 2


# --- the write-guard ships disabled -----------------------------------------

def test_the_guard_function_exists_but_only_the_capture_bridge_is_attached(db):
    """Step 5 enables it. Attaching it now would turn every existing writer into
    an exception before any of them has been converted."""
    fn = _exec(db, "SELECT count(*) AS n FROM pg_proc p JOIN pg_namespace n "
                   "ON n.oid = p.pronamespace WHERE p.proname = "
                   "'balances_are_trigger_maintained' AND n.nspname = %s", (SCHEMA,))
    assert fn[0]["n"] == 1, "the step-5 guard function was not created"

    trg = _exec(db, "SELECT count(*) AS n FROM pg_trigger t JOIN pg_class c "
                    "ON c.oid = t.tgrelid JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE c.relname = 'balances' AND NOT t.tgisinternal "
                    "AND n.nspname = %s", (SCHEMA,))
    assert trg[0]["n"] == 2, (
        "balances must have exactly the transitional delta-capture and delete-"
        "rejection triggers; the step-5 general guard remains disabled"
    )


def test_direct_writes_still_work_and_gain_an_immutable_delta(db):
    """Compatibility bridge: old application versions keep working, but no
    committed direct delta is absent from the ledger after the snapshot."""
    loan = _a_loan(db)
    before = _exec(db, "SELECT count(*) AS n FROM ledger_entries WHERE loan_id=%s "
                       "AND entry_type='legacy_direct_write'", (loan,))[0]["n"]
    _exec(db, "SELECT set_config('meridian.projecting','on',true)")
    _exec(db, "UPDATE balances SET balance = balance - 7 WHERE loan_id = %s", (loan,))
    db.commit()
    rows = _exec(db, "SELECT amount, component FROM ledger_entries WHERE loan_id=%s "
                     "AND entry_type='legacy_direct_write' ORDER BY id DESC LIMIT 1", (loan,))
    assert len(rows) == 1 and rows[0]["amount"] == -7
    assert rows[0]["component"] == "principal"
    after = _exec(db, "SELECT count(*) AS n FROM ledger_entries WHERE loan_id=%s "
                      "AND entry_type='legacy_direct_write'", (loan,))[0]["n"]
    assert after == before + 1


def test_a_balance_row_cannot_be_deleted_during_cutover(db):
    loan = _a_loan(db)
    with pytest.raises(psycopg2.errors.RaiseException):
        _exec(db, "DELETE FROM balances WHERE loan_id=%s", (loan,))
    db.rollback()
    assert _exec(db, "SELECT count(*) AS n FROM balances WHERE loan_id=%s", (loan,))[0]["n"] == 1


@pytest.mark.parametrize("entry_type,component,amount", [
    ("fee_assessed", "principal", 10),
    ("fee_assessed", "interest", 10),
    ("fee_waived", "principal", -10),
    ("disbursement", "fees", 10),
    ("adjustment", "interest", 10),
])
def test_entry_type_must_match_its_component(db, entry_type, component, amount):
    loan = _a_loan(db)
    with pytest.raises(psycopg2.errors.CheckViolation):
        _exec(db, "INSERT INTO ledger_entries(loan_id,component,amount,entry_type,actor_id,actor_role) "
                  "VALUES(%s,%s,%s,%s,1,'admin')", (loan, component, amount, entry_type))
    db.rollback()


def test_payment_allocation_must_equal_the_captured_amount(db):
    loan = _a_loan(db)
    pay = _a_payment(db, loan, "10.00")
    _exec(db, "INSERT INTO ledger_entries(loan_id,component,amount,entry_type,payment_id) "
              "VALUES(%s,'principal',-100,'payment',%s)", (loan, pay))
    with pytest.raises(psycopg2.errors.RaiseException):
        db.commit()
    db.rollback()


def test_posted_payment_amount_cannot_invalidate_immutable_allocation(db):
    loan = _a_loan(db)
    pay = _a_payment(db, loan, "10.00")
    _exec(db, "INSERT INTO ledger_entries(loan_id,component,amount,entry_type,payment_id) "
              "VALUES(%s,'principal',-10,'payment',%s)", (loan, pay))
    db.commit()

    with pytest.raises(psycopg2.errors.RaiseException):
        _exec(db, "UPDATE payments SET amount=11 WHERE id=%s", (pay,))
    db.rollback()
    assert _exec(db, "SELECT amount FROM payments WHERE id=%s", (pay,))[0]["amount"] == 10


def test_legacy_applied_payment_amount_is_immutable_during_cutover(db):
    """Match servicing's current transaction: claim payment application, then
    update balances. The bridge entry has no payment_id, so the durable
    payment_applications row must also mark the capture amount as posted."""
    loan = _a_loan(db)
    pay = _a_payment(db, loan, "10.00")
    _exec(db, "INSERT INTO payment_applications(payment_id,loan_id,amount) "
              "VALUES(%s,%s,10)", (pay, loan))
    _exec(db, "UPDATE balances SET balance=balance-10 WHERE loan_id=%s", (loan,))
    db.commit()

    assert _exec(db, "SELECT count(*) AS n FROM ledger_entries WHERE loan_id=%s "
                     "AND entry_type='legacy_direct_write'", (loan,))[0]["n"] >= 1
    with pytest.raises(psycopg2.errors.RaiseException):
        _exec(db, "UPDATE payments SET amount=11 WHERE id=%s", (pay,))
    db.rollback()
    assert _exec(db, "SELECT amount FROM payments WHERE id=%s", (pay,))[0]["amount"] == 10


def test_nullable_legacy_past_due_transitions_are_captured_as_zero_based_deltas(db):
    loan = _a_loan(db)
    _exec(db, "UPDATE balances SET past_due=NULL WHERE loan_id=%s", (loan,))
    db.commit()

    _exec(db, "UPDATE balances SET past_due=25 WHERE loan_id=%s", (loan,))
    _exec(db, "UPDATE balances SET past_due=NULL WHERE loan_id=%s", (loan,))
    db.commit()

    rows = _exec(db, "SELECT amount FROM ledger_entries WHERE loan_id=%s "
                     "AND component='fees' AND entry_type='legacy_direct_write' "
                     "ORDER BY id DESC LIMIT 2", (loan,))
    assert [row["amount"] for row in reversed(rows)] == [25, -25]


def test_a_loan_boarded_after_migration_gets_an_opening_delta(db):
    """Origination's live boarding path INSERTs loans then balances. The
    transitional bridge must cover that new row, not only updates to rows that
    happened to exist when migration 0035 took its snapshot."""
    loan = _exec(db, "INSERT INTO loans(app_id,principal,apr,term_months,status) "
                     "VALUES(NULL,725,10,12,'current') RETURNING id")[0]["id"]
    _exec(db, "INSERT INTO balances(loan_id,balance,past_due) VALUES(%s,725,15)", (loan,))
    db.commit()
    rows = _exec(db, "SELECT component, amount, entry_type FROM ledger_entries "
                     "WHERE loan_id=%s ORDER BY component", (loan,))
    assert {(r["component"], r["amount"], r["entry_type"]) for r in rows} == {
        ("principal", 725, "legacy_direct_write"),
        ("fees", 15, "legacy_direct_write"),
    }
    parity = _exec(db, "SELECT b.balance, b.past_due, "
                       "SUM(le.amount) FILTER(WHERE le.component='principal') AS principal, "
                       "SUM(le.amount) FILTER(WHERE le.component='fees') AS fees "
                       "FROM balances b JOIN ledger_entries le ON le.loan_id=b.loan_id "
                       "WHERE b.loan_id=%s GROUP BY b.balance,b.past_due", (loan,))[0]
    assert parity["balance"] == parity["principal"]
    assert parity["past_due"] == parity["fees"]


def _migration_without_transaction_wrappers():
    sql = MIGRATION.read_text(encoding="utf-8").replace("BEGIN;", "", 1)
    return "".join(sql.rsplit("COMMIT;", 1))


def test_a_payment_racing_the_backfill_is_captured_once_and_converges():
    """Pause the exact migration after it has installed the bridge and locked
    balances, start the legacy production-shaped payment transaction, then
    finish and commit the migration. The payment must wait, resume through the
    newly visible trigger, and leave parity rather than a missed snapshot delta."""
    schema = f"ledger_race_{uuid.uuid4().hex[:10]}"
    migrator = psycopg2.connect(DATABASE_URL)
    writer = psycopg2.connect(DATABASE_URL)
    check = psycopg2.connect(DATABASE_URL)
    try:
        migrator.autocommit = False
        with migrator.cursor() as cur:
            cur.execute(f"CREATE SCHEMA {schema}")
        migrator.commit()
        for name in INIT_FILES[:-1]:
            with migrator.cursor() as cur:
                cur.execute(f"SET search_path TO {schema}")
                cur.execute((INIT / name).read_text(encoding="utf-8"))
            migrator.commit()

        with migrator.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute("SELECT loan_id, balance FROM balances ORDER BY loan_id LIMIT 1")
            loan, opening = cur.fetchone()
        migrator.commit()

        sql = _migration_without_transaction_wrappers()
        marker = "-- --------------------------------------------------------------------------\n-- Back-fill:"
        prefix, suffix = sql.split(marker, 1)
        with migrator.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute(prefix)

        outcome = []
        def pay():
            try:
                with writer.cursor() as cur:
                    cur.execute(f"SET search_path TO {schema}")
                    cur.execute("INSERT INTO payments (loan_id, amount) VALUES (%s, 10) RETURNING id", (loan,))
                    payment_id = cur.fetchone()[0]
                    cur.execute("INSERT INTO payment_applications(payment_id,loan_id,amount) VALUES (%s,%s,10)",
                                (payment_id, loan))
                    cur.execute("UPDATE balances SET balance=balance-10 WHERE loan_id=%s", (loan,))
                writer.commit()
                outcome.append("committed")
            except Exception as exc:  # surfaced in the assertion below
                writer.rollback()
                outcome.append(exc)

        thread = threading.Thread(target=pay)
        thread.start()
        time.sleep(0.25)
        assert thread.is_alive(), "the payment did not block behind the migration cutover lock"

        with migrator.cursor() as cur:
            cur.execute(marker + suffix)
        migrator.commit()
        thread.join(timeout=10)
        assert outcome == ["committed"]

        with check.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute("SELECT balance FROM balances WHERE loan_id=%s", (loan,))
            assert cur.fetchone()["balance"] == opening - 10
            cur.execute("SELECT amount FROM ledger_entries WHERE loan_id=%s "
                        "AND entry_type='legacy_direct_write'", (loan,))
            assert [r["amount"] for r in cur.fetchall()] == [-10]
            cur.execute("SELECT COALESCE(SUM(amount),0) AS total FROM ledger_entries "
                        "WHERE loan_id=%s AND component='principal'", (loan,))
            assert cur.fetchone()["total"] == opening - 10

        with check.cursor() as cur:
            cur.execute(f"SET search_path TO {schema}")
            cur.execute(_migration_without_transaction_wrappers())
            cur.execute("SELECT count(*) FROM ledger_entries WHERE loan_id=%s "
                        "AND entry_type='legacy_direct_write'", (loan,))
            assert cur.fetchone()[0] == 1, "migration replay duplicated the captured delta"
        check.commit()
    finally:
        for conn in (writer, check, migrator):
            try:
                conn.rollback()
            except Exception:
                pass
        try:
            migrator.autocommit = True
            with migrator.cursor() as cur:
                cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        finally:
            writer.close(); check.close(); migrator.close()


def test_negative_legacy_state_can_be_opened_without_inventing_history(db):
    loan = _exec(db, "INSERT INTO loans(app_id,principal,apr,term_months,status) "
                     "VALUES(NULL,100,10,12,'current') RETURNING id")[0]["id"]
    _exec(db, "INSERT INTO balances(loan_id,balance,past_due) VALUES(%s,-25,-3)", (loan,))
    db.commit()
    assert _exec(db, "SELECT COALESCE(SUM(amount),0) AS n FROM ledger_entries "
                     "WHERE loan_id=%s", (loan,))[0]["n"] == -28


# --- the projection must actually project -----------------------------------


def _orphan_loan(conn):
    """A loan with no `balances` row -- the shape that exposed the defect."""
    rows = _exec(conn, "INSERT INTO loans (app_id, principal, apr, term_months, status) "
                       "VALUES (NULL, 1000, 10, 12, 'active') RETURNING id")
    return rows[0]["id"]


def test_an_entry_for_a_loan_with_no_balance_row_is_rejected(db):
    """The defect: the UPDATE matched zero rows and the INSERT still succeeded.

    The ledger recorded that money moved and no balance moved with it -- the
    projection claiming to be maintained while it is not. `balances` is derived,
    so the divergence is invisible until a parity run, and the entry is immutable
    so it cannot be corrected afterwards.
    """
    loan = _orphan_loan(db)
    db.commit()

    pay = _a_payment(db, loan)
    with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
        _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, "
                  "payment_id) "
                  "VALUES (%s, 'principal', -50.00, 'payment', %s)", (loan, pay))
    assert "expected exactly 1" in str(excinfo.value)
    db.rollback()


def test_a_rejected_entry_leaves_no_orphan(db):
    """Raising must roll the INSERT back with it. An entry that could not be
    projected is a permanent, uncorrectable record of a movement that never
    reached the borrower's balance."""
    loan = _orphan_loan(db)
    db.commit()

    pay = _a_payment(db, loan)
    with pytest.raises(psycopg2.errors.RaiseException):
        _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, "
                  "payment_id) "
                  "VALUES (%s, 'principal', -50.00, 'payment', %s)", (loan, pay))
    db.rollback()

    remaining = _exec(db, "SELECT count(*) AS n FROM ledger_entries WHERE loan_id = %s",
                      (loan,))[0]["n"]
    assert remaining == 0, "a ledger entry survived a failed projection"


def test_a_fees_entry_for_a_loan_with_no_balance_row_is_also_rejected(db):
    """Both projected components, not just principal -- the fees branch has its
    own UPDATE and would have had its own silent no-op."""
    loan = _orphan_loan(db)
    db.commit()

    with pytest.raises(psycopg2.errors.RaiseException):
        _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type) "
                  "VALUES (%s, 'fees', 25.00, 'fee_assessed')", (loan,))
    db.rollback()


def test_a_normal_entry_updates_exactly_one_balance(db):
    """Guards the guard: a check that rejected everything would satisfy the three
    tests above and break the system."""
    loan = _a_loan(db)
    before = _exec(db, "SELECT balance FROM balances WHERE loan_id = %s", (loan,))[0]["balance"]

    pay = _a_payment(db, loan, "5.00")
    _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, payment_id) "
              "VALUES (%s, 'principal', -5.00, 'payment', %s)", (loan, pay))
    db.commit()

    after = _exec(db, "SELECT balance FROM balances WHERE loan_id = %s", (loan,))[0]["balance"]
    assert after == before - 5

    touched = _exec(db, "SELECT count(*) AS n FROM balances WHERE loan_id = %s", (loan,))[0]["n"]
    assert touched == 1, "more than one balance row exists for this loan"


def test_an_interest_entry_is_still_accepted(db):
    """`interest` projects nowhere by design, so the row-count check must not
    reject it -- the ELSE branch exists precisely so a correct no-op is not
    mistaken for a failed projection."""
    loan = _a_loan(db)
    pay = _a_payment(db, loan, "7.50")
    _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, payment_id) "
              "VALUES (%s, 'interest', -7.50, 'payment', %s)", (loan, pay))
    db.commit()


def test_fresh_init_and_the_migration_enforce_it_identically(db):
    """Both schema paths carry the same function body.

    A guard present in one and absent in the other is worse than absent from
    both: whether money silently fails to move would depend on how the database
    was built.
    """
    init = (REPO / "db" / "init" / "001_schema.sql").read_text(encoding="utf-8")
    mig = MIGRATION.read_text(encoding="utf-8")
    for src, name in ((init, "db/init/001_schema.sql"), (mig, MIGRATION.name)):
        assert "GET DIAGNOSTICS projected = ROW_COUNT" in src, (
            f"{name} does not check how many balance rows the projection updated"
        )
        assert "expected exactly 1" in src, f"{name} has no row-count assertion"


# --- payment provenance: the entry must cite ITS OWN loan's payment ----------

def _two_loans_with_balances(conn):
    rows = _exec(conn, "SELECT loan_id FROM balances ORDER BY loan_id LIMIT 2")
    assert len(rows) == 2, "the seed does not carry two loans with balances"
    return rows[0]["loan_id"], rows[1]["loan_id"]


def test_an_entry_cannot_move_one_loan_using_another_loans_payment(db):
    """The reported defect, and the reason it is worse than an ordinary bug.

    `loan_id` and `payment_id` were independent foreign keys, so a row could
    cite a payment captured for loan A while projecting the movement onto loan
    B. Ledger rows are immutable: that movement could never be corrected by an
    update, so it would sit on the wrong borrower's balance permanently, and the
    only record of where it came from would point at the other borrower.
    """
    loan_a, loan_b = _two_loans_with_balances(db)
    pay_a = _a_payment(db, loan_a, "50.00")
    db.commit()

    before = _exec(db, "SELECT balance FROM balances WHERE loan_id = %s",
                   (loan_b,))[0]["balance"]

    with pytest.raises(psycopg2.errors.Error) as excinfo:
        _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, "
                  "payment_id) "
                  "VALUES (%s, 'principal', -50.00, 'payment', %s)", (loan_b, pay_a))
    db.rollback()

    # The failure names the provenance mismatch, not something downstream of it.
    assert "was captured for loan" in str(excinfo.value), (
        f"the insert failed for the wrong reason: {excinfo.value}"
    )

    after = _exec(db, "SELECT balance FROM balances WHERE loan_id = %s",
                  (loan_b,))[0]["balance"]
    assert after == before, (
        "loan B's balance moved on a payment captured for loan A -- the "
        "projection ran before the provenance was checked"
    )
    orphans = _exec(db, "SELECT count(*) AS n FROM ledger_entries WHERE payment_id = %s",
                    (pay_a,))[0]["n"]
    assert orphans == 0, "an entry citing another loan's payment survived"


def test_the_foreign_key_holds_even_without_the_trigger(db):
    """The trigger makes the failure legible and makes it happen first; the
    COMPOSITE FOREIGN KEY is the guarantee.

    A guarantee that lives only in a trigger can be dropped independently of the
    column it protects, so this drops the trigger and asserts the database still
    refuses -- and then puts it back.
    """
    loan_a, loan_b = _two_loans_with_balances(db)
    pay_a = _a_payment(db, loan_a, "60.00")
    db.commit()

    # Committed, or the rollback below would put the trigger back and the
    # recreate would then fail on a trigger that already exists -- leaving the
    # module-scoped connection aborted for every test after this one.
    _exec(db, "DROP TRIGGER ledger_entries_payment_belongs_to_loan ON ledger_entries")
    db.commit()
    try:
        with pytest.raises(psycopg2.errors.ForeignKeyViolation):
            _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, "
                      "entry_type, payment_id) "
                      "VALUES (%s, 'principal', -60.00, 'payment', %s)", (loan_b, pay_a))
    finally:
        db.rollback()
        _exec(db, "CREATE TRIGGER ledger_entries_payment_belongs_to_loan "
                  "BEFORE INSERT ON ledger_entries FOR EACH ROW "
                  "EXECUTE FUNCTION ledger_entry_payment_matches_loan()")
        db.commit()


def test_a_payment_entry_must_name_a_payment(db):
    """The other half of invariant 7, and the one the unique index cannot give.

    `UNIQUE (payment_id, component) WHERE payment_id IS NOT NULL` excludes NULLs,
    so entries with no payment_id never collide -- a retried apply would post the
    balance twice, which is exactly the idempotency the pair is supposed to
    provide.
    """
    loan = _a_loan(db)
    with pytest.raises(psycopg2.errors.CheckViolation):
        _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type) "
                  "VALUES (%s, 'principal', -12.00, 'payment')", (loan,))
    db.rollback()


@pytest.mark.parametrize("entry_type,component,amount,actor", [
    ("adjustment", "principal", "-20.00", True),
    ("fee_waived", "fees", "-20.00", True),
    ("fee_assessed", "fees", "20.00", False),
    ("disbursement", "principal", "20.00", False),
])
def test_a_non_payment_entry_may_not_carry_a_payment(db, entry_type, component,
                                                     amount, actor):
    """A non-payment entry consuming a (payment_id, component) pair would block
    the real payment entry from ever being written -- the idempotency key spent
    on something that is not the payment. An adjustment's provenance is its
    proposal (ADR 0011), never a payment."""
    loan = _a_loan(db)
    pay = _a_payment(db, loan, "20.00")
    db.commit()

    with pytest.raises(psycopg2.errors.CheckViolation):
        _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, "
                  "actor_id, actor_role, payment_id) "
                  "VALUES (%s, %s, %s, %s, %s, %s, %s)",
              (loan, component, amount, entry_type,
               1 if actor else None, "admin" if actor else None, pay))
    db.rollback()


def test_an_entry_citing_a_payment_that_does_not_exist_is_refused(db):
    loan = _a_loan(db)
    with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
        _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, "
                  "payment_id) "
                  "VALUES (%s, 'principal', -30.00, 'payment', 987654321)", (loan,))
    assert "does not exist" in str(excinfo.value)
    db.rollback()


def test_the_matching_case_still_works(db):
    """Guards the guard. A constraint that refused every payment entry would
    satisfy every test above and stop the ledger recording payments at all."""
    loan = _a_loan(db)
    before = _exec(db, "SELECT balance FROM balances WHERE loan_id = %s",
                   (loan,))[0]["balance"]
    pay = _a_payment(db, loan, "40.00")

    _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, "
              "payment_id) "
              "VALUES (%s, 'principal', -40.00, 'payment', %s)", (loan, pay))
    db.commit()

    after = _exec(db, "SELECT balance FROM balances WHERE loan_id = %s",
                  (loan,))[0]["balance"]
    assert after == before - 40
