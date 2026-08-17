"""The late fee is recorded as a ledger entry, not as a direct `balances` write.

ADR 0010 step 3, the row for `delinquency.py::assess_late_fee` in the ADR's own
writer table: `past_due`, via a `fee_assessed` entry. This was the last LIVE
direct writer -- the three remaining ones (`balance.apply_payment`,
`adjust_balance`, `waive_fee`) are unreferenced by any route.

Against real PostgreSQL, because every property here belongs to a trigger: the
projection maintains `past_due`, the append-only trigger forbids mutation, and
the row-count check is what refuses an entry that would land on no balance. A
mock would assert my own arithmetic back at me and prove none of it.
"""
import os
import pathlib
import urllib.parse

import psycopg2
import psycopg2.extras
import pytest

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against"
)

REPO = pathlib.Path(__file__).resolve().parents[3]
# 001 builds the tables and the projection; 007 defines the ledger controls and
# attaches them to `balances` -- the compatibility bridge among them. Both are
# needed for this fixture to be a fresh database rather than half of one, and
# the first version of this file applied only 001, which is what made one of the
# tests below vacuous. With no seed data 007's back-fill is a no-op.
INIT_FILES = ("001_schema.sql", "007_ledger_opening_balances.sql")
SCHEMA = "servicing_late_fee_ledger_test"

# The app's own connection, pinned to the test schema. `db.transaction()` opens
# a fresh connection from DATABASE_URL rather than reusing a module-level one
# (deliberately -- see its docstring), so the search_path has to travel in the
# URL. Setting it on some other connection would leave the code under test
# writing to `public`, and the test would silently be about the wrong database.
SCHEMA_URL = (f"{DATABASE_URL}{'&' if '?' in (DATABASE_URL or '') else '?'}"
              f"options={urllib.parse.quote(f'-csearch_path={SCHEMA}')}")


def _rate_columns(cur) -> list:
    """Every rate column this branch's `db/init` declares on `loans`.

    Read from the schema this fixture just built rather than hardcoded, and the
    reason is specific: D19 is mid-flight. `db/migrations/0038` added
    `note_rate_pct` alongside `apr` and both are currently present, with `apr`
    still NOT NULL; `0039` drops `apr` on another PR. So the answer is a LIST,
    not a choice -- during the expand window a loan must be given both, and
    afterwards only one of them exists.

    This test's subject is the ledger, not the rate. Hardcoding either name
    would make it a casualty of an unrelated migration landing, in whichever
    order the two merge. It cannot mask drift, because the schema being read is
    the one built from `db/init/001_schema.sql` moments earlier in this same
    fixture.
    """
    cur.execute(
        "SELECT column_name FROM information_schema.columns "
        " WHERE table_schema = %s AND table_name = 'loans' "
        "   AND column_name IN ('apr', 'note_rate_pct')", (SCHEMA,))
    names = sorted(r["column_name"] for r in cur.fetchall())
    assert names, "the loans table has no rate column at all -- the schema is wrong"
    return names


@pytest.fixture
def schema():
    """A real schema, built from `db/init/001_schema.sql`.

    Not a hand-written subset. The projection trigger, the append-only trigger
    and `ledger_actor_required` are the things under test here, and a fixture
    that declared its own `balances`/`ledger_entries` would be proving them
    against a shape production never has -- which is exactly the objection that
    was upheld against `test_no_card_data_on_either_schema_path` before
    `db/tests/migration_paths.py` existed.

    Applying only part of `db/init` is the same mistake in miniature, and this
    fixture made it: it built 001 alone, so the ledger controls 007 attaches
    were absent and a test that depended on one of them passed for no reason.
    """
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(f"SET search_path TO {SCHEMA}")
        for name in INIT_FILES:
            cur.execute((REPO / "db" / "init" / name).read_text(encoding="utf-8"))
    yield conn
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.close()


@pytest.fixture
def loan(schema, monkeypatch):
    """One loan with a balances row.

    Committed rather than held open: `assess_late_fee` opens its OWN connection,
    so anything this fixture left in an uncommitted transaction would be
    invisible to the code under test, and the test would exercise a loan that,
    as far as the function is concerned, does not exist.
    """
    from app import db as app_db
    monkeypatch.setattr(app_db, "DATABASE_URL", SCHEMA_URL)

    with schema.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        rates = _rate_columns(cur)
        rate_cols = ", ".join(rates)
        rate_vals = ", ".join(["7.99"] * len(rates))
        cur.execute("INSERT INTO applicants (name) VALUES ('Fee Fixture') RETURNING id")
        applicant = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO applications (applicant_id, amount, term_months, status) "
            "VALUES (%s, 9000, 24, 'funded') RETURNING id", (applicant,))
        app_id = cur.fetchone()["id"]
        cur.execute(
            f"INSERT INTO loans (app_id, applicant_name, principal, {rate_cols}, "
            f"term_months) VALUES (%s, 'Fee Fixture', 9000, {rate_vals}, 24) "
            f"RETURNING id",
            (app_id,))
        loan_id = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO balances (loan_id, balance, past_due) VALUES (%s, 9000, 0)",
            (loan_id,))
    return loan_id


def _rows(sql, params):
    conn = psycopg2.connect(SCHEMA_URL)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def _entries(loan_id):
    return _rows(
        "SELECT component, amount, entry_type, reason, actor_id, actor_role, "
        "       payment_id "
        "  FROM ledger_entries WHERE loan_id = %s ORDER BY id", (loan_id,))


def _fee_entries(loan_id):
    """Entries against `fees` only.

    The fixture's own `INSERT INTO balances` is a direct write, so the
    compatibility bridge records a `principal` `legacy_direct_write` for the
    opening amount -- correctly, and exactly as a legacy row entering the system
    would. That entry is fixture setup, not the subject, so assertions about the
    fee filter it out rather than counting every row on the loan.
    """
    return [e for e in _entries(loan_id) if e["component"] == "fees"]


def _past_due(loan_id):
    return float(_rows("SELECT past_due FROM balances WHERE loan_id = %s",
                       (loan_id,))[0]["past_due"])


# --- the conversion itself ---------------------------------------------------

def test_it_writes_a_fee_assessed_entry(loan):
    from app import delinquency

    delinquency.assess_late_fee(loan)

    entries = _fee_entries(loan)
    assert len(entries) == 1, f"expected exactly one fee entry, got {entries}"
    entry = entries[0]
    assert entry["entry_type"] == "fee_assessed"
    assert entry["component"] == "fees"
    assert float(entry["amount"]) == delinquency.LATE_FEE_FLAT


def test_it_survives_the_write_guard_that_forbids_direct_balance_writes(loan, schema):
    """The point of the change, and the assertion that fails if the function
    still runs its own UPDATE.

    ADR 0010's step-5 guard (`balances_are_trigger_maintained`) rejects any
    write to `balances` that did not come from the projection. It ships as a
    function with **no trigger attached**, because attaching it while a
    legacy writer remains would break that writer -- which is the whole reason
    this conversion exists. So the test attaches it for the duration, which is
    the only way to prove the negative: the fee reached `past_due` WITHOUT a
    direct write.

    **This was added because another test here was vacuous, and the sequence is
    the point.** That test asserted no `legacy_direct_write` entry appeared,
    relying on the compatibility bridge to capture a direct write. Mutation
    testing -- restoring the direct `UPDATE` -- left it passing, because this
    fixture applied `db/init/001_schema.sql` alone and the bridge is attached by
    `db/init/007`. An absent control cannot witness the defect it is being used
    to detect. The fixture now applies both files, so that test bites too; this
    one is kept because it proves the property directly rather than through a
    second control.
    """
    from app import delinquency

    with schema.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(
            "CREATE TRIGGER balances_guard_direct_writes "
            "BEFORE INSERT OR UPDATE ON balances "
            "FOR EACH ROW EXECUTE FUNCTION balances_are_trigger_maintained()")
    try:
        # Guard the guard: the trigger must actually bite, or this test proves
        # nothing about how the fee got there.
        with pytest.raises(psycopg2.Error):
            with schema.cursor() as cur:
                cur.execute(f"SET search_path TO {SCHEMA}")
                cur.execute("UPDATE balances SET past_due = past_due + 1 "
                            " WHERE loan_id = %s", (loan,))

        before = _past_due(loan)
        delinquency.assess_late_fee(loan)
        assert _past_due(loan) == pytest.approx(
            before + delinquency.LATE_FEE_FLAT)
    finally:
        with schema.cursor() as cur:
            cur.execute(f"SET search_path TO {SCHEMA}")
            cur.execute("DROP TRIGGER IF EXISTS balances_guard_direct_writes "
                        "ON balances")


def test_it_writes_no_legacy_direct_write_entry(loan):
    """The compatibility bridge must have nothing to capture.

    `balances_capture_legacy_delta` (attached by `db/init/007` on a fresh
    database and by `db/migrations/0035` on an upgraded one) records a direct
    `balances` write as a `legacy_direct_write` entry -- an entry that says the
    column moved and nothing about what moved it. Its presence here would mean
    the conversion did not happen, however right `past_due` looks afterwards.

    This test was vacuous in its first form: the fixture applied
    `db/init/001_schema.sql` alone, so the bridge was never attached and the
    assertion held no matter what the code did. Mutation testing caught it. The
    fixture applies 007 too now, so the bridge exists and this bites.
    """
    from app import delinquency

    # Guard the guard: the control this test depends on must actually be here.
    attached = _rows(
        "SELECT 1 FROM pg_trigger t JOIN pg_class c ON c.oid = t.tgrelid "
        "  JOIN pg_namespace n ON n.oid = c.relnamespace "
        " WHERE n.nspname = %s AND c.relname = 'balances' "
        "   AND t.tgname = 'balances_capture_legacy_delta'", (SCHEMA,))
    assert attached, (
        "the compatibility bridge is not attached, so this test cannot observe "
        "a direct write and would pass regardless"
    )

    delinquency.assess_late_fee(loan)

    kinds = [e["entry_type"] for e in _fee_entries(loan)]
    assert "legacy_direct_write" not in kinds, (
        f"the fee still reached past_due by a direct UPDATE: {kinds}"
    )


def test_past_due_moves_by_exactly_the_fee(loan):
    from app import delinquency

    before = _past_due(loan)
    returned = delinquency.assess_late_fee(loan)
    after = _past_due(loan)

    assert after == pytest.approx(before + delinquency.LATE_FEE_FLAT)
    assert returned == pytest.approx(after), (
        "the returned past_due disagrees with the stored one"
    )


def test_the_projection_is_what_moves_past_due(loan):
    """Not the function. Inserting the same entry by hand must move `past_due`
    identically -- if it does not, `assess_late_fee` is still doing the update
    itself and the entry is decorative."""
    from app import delinquency

    before = _past_due(loan)
    conn = psycopg2.connect(SCHEMA_URL)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO ledger_entries (loan_id, component, amount, entry_type) "
                "VALUES (%s, 'fees', %s, 'fee_assessed')",
                (loan, delinquency.LATE_FEE_FLAT))
    finally:
        conn.close()

    assert _past_due(loan) == pytest.approx(before + delinquency.LATE_FEE_FLAT)


def test_repeated_assessments_accumulate(loan):
    """A fee is not idempotent and must not be. Two assessments are two fees --
    unlike a payment, which carries a payment_id and is deduplicated by the
    unique index. The absence of a payment_id here is deliberate."""
    from app import delinquency

    delinquency.assess_late_fee(loan)
    delinquency.assess_late_fee(loan)

    entries = _fee_entries(loan)
    assert len(entries) == 2
    assert _past_due(loan) == pytest.approx(2 * delinquency.LATE_FEE_FLAT)


def test_the_entry_names_no_actor(loan):
    """A machine-originated fee has no human behind it. `fee_assessed` is exempt
    from `ledger_actor_required` for that reason, and inventing an actor to fill
    the column would record a person who did not act
    (`specs/0002-maker-checker-self-approval.md` §8)."""
    from app import delinquency

    delinquency.assess_late_fee(loan)

    entry = _fee_entries(loan)[0]
    assert entry["actor_id"] is None
    assert entry["actor_role"] is None
    assert entry["payment_id"] is None


def test_the_entry_records_why(loan):
    """`reason` is the difference between this and the `legacy_direct_write` it
    replaces: an auditor reading the ledger can tell a late fee from any other
    movement of `past_due` without joining anything."""
    from app import delinquency

    delinquency.assess_late_fee(loan)

    assert (_fee_entries(loan)[0]["reason"] or "").strip(), (
        "the entry carries no reason, so the ledger records that past_due moved "
        "and not that it was a late fee"
    )


def test_the_entry_cannot_be_altered_afterwards(loan):
    """Append-only. Not a property of this function, but the reason writing the
    ledger is worth more than writing the column: the record of the fee cannot
    be edited away later."""
    from app import delinquency

    delinquency.assess_late_fee(loan)

    conn = psycopg2.connect(SCHEMA_URL)
    conn.autocommit = True
    try:
        with conn.cursor() as cur, pytest.raises(psycopg2.Error):
            cur.execute("UPDATE ledger_entries SET amount = 1 WHERE loan_id = %s",
                        (loan,))
    finally:
        conn.close()


# --- the behaviour change, stated rather than smuggled -----------------------

def test_a_loan_with_no_balances_row_is_refused(schema, monkeypatch):
    """**This is a behaviour change and it is the honest half of the PR.**

    The direct-write version read `past_due` as `rows[0] if rows else 0.0`, ran
    an UPDATE matching zero rows, and returned 35.0 -- so the API said a fee had
    been assessed, the log line said so, and no balance moved. Nothing errored.

    The projection refuses to record a movement that lands on no balance row, so
    this now raises. A refusal a caller can see is worth more than a number that
    is quietly about nothing.
    """
    from app import db as app_db, delinquency
    monkeypatch.setattr(app_db, "DATABASE_URL", SCHEMA_URL)

    conn = schema
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        rates = _rate_columns(cur)
        rate_cols = ", ".join(rates)
        rate_vals = ", ".join(["7.99"] * len(rates))
        cur.execute("INSERT INTO applicants (name) VALUES ('No Balance') RETURNING id")
        applicant = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO applications (applicant_id, amount, term_months, status) "
            "VALUES (%s, 500, 12, 'funded') RETURNING id", (applicant,))
        app_id = cur.fetchone()["id"]
        cur.execute(
            f"INSERT INTO loans (app_id, applicant_name, principal, {rate_cols}, "
            f"term_months) VALUES (%s, 'No Balance', 500, {rate_vals}, 12) "
            f"RETURNING id",
            (app_id,))
        loan_id = cur.fetchone()["id"]
    try:
        # Guard the guard: the loan must genuinely have no balances row, or this
        # test passes for the wrong reason.
        assert not _rows("SELECT 1 FROM balances WHERE loan_id = %s", (loan_id,))

        with pytest.raises(delinquency.LoanHasNoBalances):
            delinquency.assess_late_fee(loan_id)

        assert not _entries(loan_id), (
            "an entry survived for a loan whose balance never moved -- the "
            "ledger now permanently records a fee that reached nobody"
        )
    finally:
        # The `schema` fixture drops the whole schema, so no row-by-row cleanup
        # and no closing the connection it owns.
        pass
