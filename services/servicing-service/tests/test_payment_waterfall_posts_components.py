"""A payment lands as one ledger entry per component (D14), against real Postgres.

`test_payment_waterfall.py` proves the arithmetic. This proves the part only a
database can: that the split actually reaches `ledger_entries`, that the
projection moves BOTH `balance` and `past_due` from it, and that
`ledger_payment_allocation_exact` -- the deferred constraint trigger requiring a
payment's entries to sum to the captured amount -- is satisfied by what the
waterfall writes rather than by luck.

Built from `db/init` (001 + 007) so the triggers under test are the ones the
database really has, not a hand-written subset.
"""
import datetime
import os
import pathlib
import urllib.parse
from decimal import Decimal

import psycopg2
import psycopg2.extras
import pytest

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against"
)

REPO = pathlib.Path(__file__).resolve().parents[3]
INIT_FILES = ("001_schema.sql", "007_ledger_opening_balances.sql")
SCHEMA = "servicing_waterfall_test"
SCHEMA_URL = (f"{DATABASE_URL}{'&' if '?' in (DATABASE_URL or '') else '?'}"
              f"options={urllib.parse.quote(f'-csearch_path={SCHEMA}')}")

PRINCIPAL = Decimal("18000.00")
RATE = Decimal("7.99")
TERM = 48
REGULAR = Decimal("439.35")
FINAL = Decimal("439.24")
# Opened 45 days ago, so EXACTLY ONE period has fallen due. Anchored to today
# rather than to a fixed date on purpose: a hardcoded 2026-01-15 meant seven
# periods had elapsed by the time these tests ran, the accrued interest exceeded
# the payment, and every payment went entirely to interest. That is the policy
# order behaving correctly -- and it made the tests assert nothing about
# principal, which is half of what the waterfall does.
OPENED = datetime.date.today() - datetime.timedelta(days=45)


@pytest.fixture
def db(monkeypatch):
    from app import db as app_db
    monkeypatch.setattr(app_db, "DATABASE_URL", SCHEMA_URL)

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


def _rows(sql, params=()):
    conn = psycopg2.connect(SCHEMA_URL)
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)
            return cur.fetchall()
    finally:
        conn.close()


def _loan(db, *, past_due=Decimal("0.00"), schedule=True, balance=PRINCIPAL):
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("INSERT INTO applicants (name) VALUES ('Waterfall') RETURNING id")
        applicant = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO applications (applicant_id, amount, term_months, status) "
            "VALUES (%s, %s, %s, 'funded') RETURNING id",
            (applicant, PRINCIPAL, TERM))
        app_id = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO loans (app_id, applicant_name, principal, note_rate_pct, "
            "term_months, regular_payment, regular_payment_count, final_payment, "
            "schedule_version, opened_at) "
            "VALUES (%s, 'Waterfall', %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (app_id, PRINCIPAL, RATE, TERM,
             REGULAR if schedule else None,
             TERM - 1 if schedule else None,
             FINAL if schedule else None,
             "B1" if schedule else None,
             OPENED))
        loan_id = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO balances (loan_id, balance, past_due) VALUES (%s, %s, %s)",
            (loan_id, balance, past_due))
    return loan_id


def _payment(db, loan_id, amount):
    with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(
            "INSERT INTO payments (loan_id, amount, method, auth_status) "
            "VALUES (%s, %s, 'card', 'captured') RETURNING id", (loan_id, amount))
        return cur.fetchone()["id"]


def _entries(loan_id):
    return _rows("SELECT component, amount, entry_type FROM ledger_entries "
                 " WHERE loan_id = %s AND entry_type = 'payment' "
                 " ORDER BY component", (loan_id,))


def _balances(loan_id):
    row = _rows("SELECT balance, past_due FROM balances WHERE loan_id = %s",
                (loan_id,))[0]
    return row["balance"], row["past_due"]


# --- the split reaches the ledger ---------------------------------------------

def test_a_payment_with_fees_owed_writes_one_entry_per_component(db):
    """The defect D14 names. A borrower carrying a $35 late fee used to have the
    whole payment reduce principal while the fee stayed owed."""
    from app import balance

    loan_id = _loan(db, past_due=Decimal("35.00"))
    payment_id = _payment(db, loan_id, Decimal("500.00"))

    balance.apply_payment_once(payment_id, loan_id, Decimal("500.00"))

    entries = {e["component"]: e["amount"] for e in _entries(loan_id)}
    assert entries["fees"] == Decimal("-35.00"), (
        f"fees were not paid first: {entries}")
    assert "principal" in entries and "interest" in entries
    assert sum(entries.values()) == Decimal("-500.00"), (
        "the entries do not sum to the payment")


def test_the_projection_moves_both_columns(db):
    """`past_due` and `balance` are separate projections of the same payment.
    Only a database can show that one payment moved both."""
    from app import balance

    loan_id = _loan(db, past_due=Decimal("35.00"))
    before_balance, before_past_due = _balances(loan_id)
    payment_id = _payment(db, loan_id, Decimal("500.00"))

    balance.apply_payment_once(payment_id, loan_id, Decimal("500.00"))

    after_balance, after_past_due = _balances(loan_id)
    assert after_past_due == before_past_due - Decimal("35.00"), (
        "the fee was not cleared from past_due")
    assert after_balance < before_balance, "principal did not move"
    moved = (before_balance - after_balance) + (before_past_due - after_past_due)
    assert moved <= Decimal("500.00"), "more moved than was paid"


def test_a_payment_that_only_covers_fees_leaves_principal_untouched(db):
    from app import balance

    loan_id = _loan(db, past_due=Decimal("35.00"))
    before_balance, _ = _balances(loan_id)
    payment_id = _payment(db, loan_id, Decimal("20.00"))

    balance.apply_payment_once(payment_id, loan_id, Decimal("20.00"))

    after_balance, after_past_due = _balances(loan_id)
    assert after_balance == before_balance, (
        "principal moved while fees were still owed")
    assert after_past_due == Decimal("15.00")
    assert [e["component"] for e in _entries(loan_id)] == ["fees"]


def test_a_loan_with_nothing_but_principal_owed_still_posts_one_entry(db):
    """The ordinary case, and the regression that matters most: the waterfall
    must not change what a current borrower's payment does."""
    from app import balance

    loan_id = _loan(db, schedule=False)
    before_balance, _ = _balances(loan_id)
    payment_id = _payment(db, loan_id, Decimal("439.35"))

    balance.apply_payment_once(payment_id, loan_id, Decimal("439.35"))

    entries = _entries(loan_id)
    assert [e["component"] for e in entries] == ["principal"]
    assert _balances(loan_id)[0] == before_balance - Decimal("439.35")


def test_interest_is_billed_from_the_contract_not_invented(db):
    """The interest entry must equal the schedule's own figure for the elapsed
    periods -- not a number this code derived by some other route."""
    from app import balance, schedule as sched

    loan_id = _loan(db)
    payment_id = _payment(db, loan_id, Decimal("500.00"))
    balance.apply_payment_once(payment_id, loan_id, Decimal("500.00"))

    entries = {e["component"]: -e["amount"] for e in _entries(loan_id)}
    rows = sched.amortization_from_contract(
        PRINCIPAL, RATE, TERM, REGULAR, FINAL, start=OPENED)
    elapsed = [r for r in rows
               if datetime.date.fromisoformat(r["due_date"]) <= datetime.date.today()]
    expected = sum(Decimal(str(r["interest"])) for r in elapsed)
    assert elapsed, "no periods have elapsed -- the case is vacuous"
    assert entries.get("interest") == expected, (
        f"interest {entries.get('interest')} does not match the contract's "
        f"{expected}")


# --- the refusals reach the database boundary intact --------------------------

def test_an_overpayment_is_refused_and_writes_nothing(db):
    from app import balance, waterfall

    loan_id = _loan(db, balance=Decimal("100.00"), schedule=False)
    payment_id = _payment(db, loan_id, Decimal("5000.00"))

    with pytest.raises(waterfall.PaymentExceedsAmountOwed):
        balance.apply_payment_once(payment_id, loan_id, Decimal("5000.00"))

    assert not _entries(loan_id), "an entry survived a refused payment"
    assert _balances(loan_id)[0] == Decimal("100.00"), "the balance moved"
    assert not _rows("SELECT 1 FROM payment_applications WHERE payment_id = %s",
                     (payment_id,)), (
        "the idempotency marker survived, so a corrected retry would be "
        "silently skipped forever")


def test_the_allocation_constraint_agrees_with_the_waterfall(db):
    """`ledger_payment_allocation_exact` requires a payment's entries to sum to
    the captured amount, deferred to commit. It is what would catch an
    allocation that silently lost a cent -- so this asserts the waterfall
    satisfies a control it does not itself own."""
    from app import balance

    loan_id = _loan(db, past_due=Decimal("35.00"))
    payment_id = _payment(db, loan_id, Decimal("500.00"))
    balance.apply_payment_once(payment_id, loan_id, Decimal("500.00"))

    captured = _rows("SELECT amount FROM payments WHERE id = %s",
                     (payment_id,))[0]["amount"]
    allocated = -sum(e["amount"] for e in _entries(loan_id))
    assert allocated == captured


def test_a_replayed_payment_does_not_post_the_split_twice(db):
    """Idempotency still holds now that one payment writes several rows."""
    from app import balance

    loan_id = _loan(db, past_due=Decimal("35.00"))
    payment_id = _payment(db, loan_id, Decimal("500.00"))

    balance.apply_payment_once(payment_id, loan_id, Decimal("500.00"))
    first = _entries(loan_id)
    _, applied = balance.apply_payment_once(payment_id, loan_id, Decimal("500.00"))

    assert applied is False
    assert _entries(loan_id) == first, "a replay wrote the split a second time"


# --- what can put an `interest` entry in the ledger ---------------------------
#
# `interest_owed` deducts the sum of ALL `interest` entries, not just payments.
# I raised that against myself as a review question: if a staff adjustment could
# credit interest, would a later payment over-allocate to principal?
#
# It cannot happen, and the reason is the schema rather than anything in
# `waterfall.py`. These two tests pin the invariant the derivation rests on, so
# that if a future change makes interest adjustments possible, the thing that
# breaks is a test naming this assumption rather than a borrower's allocation.


def test_an_interest_entry_cannot_be_written_without_an_approved_proposal(db):
    """An `adjustment` must name the `pending_movements` row that authorised it
    (ADR 0011). So no interest entry can appear by a direct write."""
    with pytest.raises(psycopg2.Error) as exc:
        with db.cursor() as cur:
            cur.execute(f"SET search_path TO {SCHEMA}")
            cur.execute(
                "INSERT INTO ledger_entries "
                "(loan_id, component, amount, entry_type, reason, actor_id, "
                " actor_role) "
                "VALUES (%s, 'interest', -10.00, 'adjustment', 't', 1, 'admin')",
                (_loan(db),))
    assert "proposal" in str(exc.value).lower()


def test_an_interest_adjustment_cannot_even_be_proposed(db):
    """And the proposal route is closed too: `pending_component` allows an
    adjustment against principal or fees only.

    Together with the test above, that means the ONLY entry type that can put an
    `interest` row in the ledger is a payment -- which is what makes
    `interest_owed`'s deduction correct: it can only ever be subtracting
    interest a borrower actually paid.
    """
    loan_id = _loan(db)
    with pytest.raises(psycopg2.Error) as exc:
        with db.cursor() as cur:
            cur.execute(f"SET search_path TO {SCHEMA}")
            cur.execute(
                "INSERT INTO pending_movements "
                "(loan_id, component, amount, entry_type, reason, requested_by, "
                " requested_role) "
                "VALUES (%s, 'interest', -10.00, 'adjustment', 'test', 1, 'admin')",
                (loan_id,))
    message = str(exc.value)
    # Named specifically. An earlier version of this test used the wrong column
    # name and failed with `UndefinedColumn` -- it would have "passed" against a
    # looser assertion while proving nothing about the constraint.
    assert "pending_component" in message, (
        f"refused, but not by the constraint under test: {message}")


def test_a_payment_is_the_only_thing_that_wrote_interest(db):
    """Guard the guard: the two refusals above are only meaningful if a payment
    genuinely does write an interest entry."""
    from app import balance

    loan_id = _loan(db)
    payment_id = _payment(db, loan_id, Decimal("500.00"))
    balance.apply_payment_once(payment_id, loan_id, Decimal("500.00"))

    kinds = _rows("SELECT DISTINCT entry_type FROM ledger_entries "
                  " WHERE loan_id = %s AND component = 'interest'", (loan_id,))
    assert [k["entry_type"] for k in kinds] == ["payment"]
