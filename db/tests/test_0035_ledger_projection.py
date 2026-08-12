"""ADR 0010 step 2: the ledger, its projection, and the back-fill that seeds it.

Against real PostgreSQL, in a throwaway schema built from `db/init` plus the
migration, because every invariant here is enforced by the database and none of
them can be tested against a mock: a trigger that does not fire, a CHECK that
does not hold and a unique index that does not exist all look identical from
Python.

What this migration is responsible for -- the binding invariants from ADR 0010:

  1. entries are immutable
  2. an entry is a signed delta, never a total
  3. `balances` is written by the projection and nothing else
  4. the sign is keyed to what the borrower owes
  5. a human-directed entry names the human
  7. exactly one entry per (payment_id, component)

and the back-fill: the projection must equal the ledger sum for every loan, and
running the migration twice must not double anything.
"""
import os
import pathlib

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
    """Guards the guard: a migration that seeded nothing satisfies both checks
    above by comparing zero against zero."""
    n = _exec(db, "SELECT count(*) AS n FROM ledger_entries "
                  "WHERE entry_type = 'opening_balance'")[0]["n"]
    assert n > 0, "the back-fill produced no opening entries -- nothing was verified"


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

    _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type) "
              "VALUES (%s, 'principal', -25.00, 'payment')", (loan,))
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

    _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type) "
              "VALUES (%s, 'principal', -10.00, 'payment'), "
              "       (%s, 'principal', -15.00, 'payment')", (loan, loan))
    db.commit()

    after = _exec(db, "SELECT balance FROM balances WHERE loan_id = %s", (loan,))[0]["balance"]
    assert after == before - 25, "the two entries did not both apply"


def test_interest_projects_nowhere(db):
    loan = _a_loan(db)
    b = _exec(db, "SELECT balance, past_due FROM balances WHERE loan_id = %s", (loan,))[0]

    _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type) "
              "VALUES (%s, 'interest', 5.00, 'fee_assessed')", (loan,))
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
    entry = _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type) "
                      "VALUES (%s, 'principal', -1.00, 'payment') RETURNING id", (loan,))[0]["id"]
    db.commit()

    with pytest.raises(psycopg2.errors.RaiseException):
        _exec(db, statement, (entry,))
    db.rollback()


# --- invariant 4: the sign is keyed to the effect ---------------------------

@pytest.mark.parametrize("entry_type, amount", [
    ("payment", 10.00),          # a payment that increases what is owed
    ("fee_assessed", -10.00),    # a fee that reduces it
    ("opening_balance", -10.00),
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
    _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type) "
              "VALUES (%s, 'principal', -3.00, 'payment')", (loan,))
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

def test_the_guard_function_exists_but_is_not_attached(db):
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
    assert trg[0]["n"] == 0, (
        "a trigger is already attached to balances -- every unconverted writer "
        "would start raising"
    )


def test_direct_writes_to_balances_still_work_at_this_step(db):
    """Deliberate. This migration changes no behaviour; step 3 converts the
    writers and step 5 forbids the direct path. Asserting it here means a future
    change that enables the guard early fails loudly rather than in production."""
    loan = _a_loan(db)
    _exec(db, "UPDATE balances SET balance = balance WHERE loan_id = %s", (loan,))
    db.commit()


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

    with pytest.raises(psycopg2.errors.RaiseException) as excinfo:
        _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type) "
                  "VALUES (%s, 'principal', -50.00, 'payment')", (loan,))
    assert "expected exactly 1" in str(excinfo.value)
    db.rollback()


def test_a_rejected_entry_leaves_no_orphan(db):
    """Raising must roll the INSERT back with it. An entry that could not be
    projected is a permanent, uncorrectable record of a movement that never
    reached the borrower's balance."""
    loan = _orphan_loan(db)
    db.commit()

    with pytest.raises(psycopg2.errors.RaiseException):
        _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type) "
                  "VALUES (%s, 'principal', -50.00, 'payment')", (loan,))
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

    _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type) "
              "VALUES (%s, 'principal', -5.00, 'payment')", (loan,))
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
    _exec(db, "INSERT INTO ledger_entries (loan_id, component, amount, entry_type) "
              "VALUES (%s, 'interest', 7.50, 'fee_assessed')", (loan,))
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
