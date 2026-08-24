"""What the review-item schema guarantees on its own, against real PostgreSQL.

`db/migrations/0045` exists because the client's decision of 2026-08-24 replaced
D22's deferral with a review-only contract. The properties below are the ones a
mock cannot check: a UNIQUE that does not dedupe, a CHECK that admits a fourth
disposition, and a trigger that silently allows a rewritten answer are all
invisible to a fake database, and each of them is one of the guarantees the
client's wording depends on.

**The table records an ASK, not an answer about money.** Nothing here moves a
balance, writes a ledger entry, or gives anything permission to. The tests assert
that too, because "a review queue that cannot move money" is a claim worth
holding to rather than repeating.
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
SCHEMA = "review_items_test"

INIT_FILES = ("001_schema.sql", "004_decision_events.sql", "005_manual_reviews.sql",
              "006_decision_attempts.sql", "007_ledger_opening_balances.sql")


@pytest.fixture
def db():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
    conn.commit()
    for name in INIT_FILES:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {SCHEMA}")
            cur.execute((INIT / name).read_text(encoding="utf-8"))
        conn.commit()
    yield conn
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.commit()
    conn.close()


def _cursor(conn):
    cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute(f"SET search_path TO {SCHEMA}")
    return cur


@pytest.fixture
def payments(db):
    """Two captured payments on one loan, and the loan they belong to."""
    with _cursor(db) as cur:
        cur.execute("INSERT INTO applicants (name) VALUES ('Test Borrower') RETURNING id")
        applicant = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO applications (applicant_id, amount, term_months, status) "
            "VALUES (%s, 5000, 24, 'funded') RETURNING id", (applicant,))
        application = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO loans (app_id, applicant_name, principal, note_rate_pct, "
            "term_months, status) VALUES (%s, 'Test Borrower', 5000.00, 7.990, 24, "
            "'current') RETURNING id", (application,))
        loan_id = cur.fetchone()["id"]
        cur.execute(
            "INSERT INTO balances (loan_id, balance, past_due) "
            "VALUES (%s, 5000.00, 0.00)", (loan_id,))

        ids = []
        for key in ("idem-a", "idem-b"):
            cur.execute(
                "INSERT INTO payments (loan_id, amount, method, idempotency_key, "
                "auth_status, captured_at, source_ref, capture_source) "
                "VALUES (%s, 250.00, 'card', %s, 'captured', now(), "
                "'src_mock_test', 'processor') RETURNING id",
                (loan_id, key))
            ids.append(cur.fetchone()["id"])
    db.commit()
    return {"loan_id": loan_id, "payments": ids}


def _flag(conn, payments, *, signal="heuristic_30_minute_candidate",
          payment_index=0, related_index=1):
    with _cursor(conn) as cur:
        cur.execute(
            "INSERT INTO reconciliation_review_items "
            "(signal_type, payment_id, related_payment_id, loan_id, correlation_ref) "
            "VALUES (%s, %s, %s, %s, 'pay_test') RETURNING id",
            (signal, payments["payments"][payment_index],
             payments["payments"][related_index], payments["loan_id"]))
        return cur.fetchone()["id"]


# --------------------------------------------------------------------------
# The signal vocabulary is exactly the client's three.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("signal", [
    "exact_provider_transaction_id",
    "exact_idempotency_key",
    "heuristic_30_minute_candidate",
])
def test_the_three_authorised_signal_types_are_accepted(db, payments, signal):
    assert _flag(db, payments, signal=signal)
    db.commit()


def test_a_signal_type_nobody_authorised_is_refused(db, payments):
    """Including a name that presumes the answer. "duplicate_confirmed" is a
    conclusion, and the client was explicit that a flag is not one."""
    for invented in ("duplicate_confirmed", "probable_duplicate", "fraud"):
        with pytest.raises(psycopg2.errors.CheckViolation):
            _flag(db, payments, signal=invented)
        db.rollback()


# --------------------------------------------------------------------------
# One item per observation. The client's "do not flood the queue" concern.
# --------------------------------------------------------------------------

def test_the_same_signal_on_the_same_payment_cannot_be_recorded_twice(db, payments):
    _flag(db, payments)
    db.commit()

    with pytest.raises(psycopg2.errors.UniqueViolation):
        _flag(db, payments)
    db.rollback()


def test_a_different_signal_on_the_same_payment_is_a_separate_item(db, payments):
    """One payment can raise two different observations -- an exact key repeat
    and a heuristic resemblance are different things for a human to weigh."""
    _flag(db, payments, signal="exact_idempotency_key")
    _flag(db, payments, signal="heuristic_30_minute_candidate")
    db.commit()

    with _cursor(db) as cur:
        cur.execute("SELECT count(*)::int AS n FROM reconciliation_review_items "
                    "WHERE payment_id = %s", (payments["payments"][0],))
        assert cur.fetchone()["n"] == 2


# --------------------------------------------------------------------------
# Only the three authorised dispositions, and only as a complete answer.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("disposition", [
    "confirmed_duplicate",
    "legitimate_distinct_payment",
    "requires_further_review",
])
def test_the_three_authorised_dispositions_are_accepted(db, payments, disposition):
    item = _flag(db, payments)
    db.commit()

    with _cursor(db) as cur:
        cur.execute(
            "UPDATE reconciliation_review_items SET status = 'reviewed', "
            "disposition = %s, reviewed_at = now(), reviewed_by = '7', "
            "reviewed_by_role = 'csr' WHERE id = %s",
            (disposition, item))
    db.commit()


def test_a_disposition_nobody_authorised_is_refused(db, payments):
    item = _flag(db, payments)
    db.commit()

    for invented in ("reversed", "refunded", "blocked", "invalid"):
        with pytest.raises(psycopg2.errors.CheckViolation):
            with _cursor(db) as cur:
                cur.execute(
                    "UPDATE reconciliation_review_items SET status = 'reviewed', "
                    "disposition = %s, reviewed_at = now(), reviewed_by = '7' "
                    "WHERE id = %s", (invented, item))
        db.rollback()


def test_a_reviewed_item_must_name_its_reviewer_and_time(db, payments):
    """There is no half-reviewed state: a disposition with nobody behind it is
    not evidence."""
    item = _flag(db, payments)
    db.commit()

    with pytest.raises(psycopg2.errors.CheckViolation):
        with _cursor(db) as cur:
            cur.execute(
                "UPDATE reconciliation_review_items SET status = 'reviewed', "
                "disposition = 'confirmed_duplicate' WHERE id = %s", (item,))
    db.rollback()


def test_an_open_item_cannot_carry_a_disposition(db, payments):
    item = _flag(db, payments)
    db.commit()

    with pytest.raises(psycopg2.errors.CheckViolation):
        with _cursor(db) as cur:
            cur.execute(
                "UPDATE reconciliation_review_items SET disposition = "
                "'legitimate_distinct_payment' WHERE id = %s", (item,))
    db.rollback()


# --------------------------------------------------------------------------
# The human's answer, and the observation it answers, are both immutable.
# --------------------------------------------------------------------------

def test_a_recorded_disposition_cannot_be_rewritten(db, payments):
    item = _flag(db, payments)
    db.commit()
    with _cursor(db) as cur:
        cur.execute(
            "UPDATE reconciliation_review_items SET status = 'reviewed', "
            "disposition = 'legitimate_distinct_payment', reviewed_at = now(), "
            "reviewed_by = '7', reviewed_by_role = 'csr' WHERE id = %s", (item,))
    db.commit()

    with pytest.raises(psycopg2.errors.RaiseException):
        with _cursor(db) as cur:
            cur.execute(
                "UPDATE reconciliation_review_items SET disposition = "
                "'confirmed_duplicate' WHERE id = %s", (item,))
    db.rollback()

    with _cursor(db) as cur:
        cur.execute("SELECT disposition FROM reconciliation_review_items WHERE id = %s",
                    (item,))
        assert cur.fetchone()["disposition"] == "legitimate_distinct_payment"


def test_the_observation_itself_cannot_be_edited(db, payments):
    """Rewriting the signal or its subject would make the row describe something
    that was never noticed."""
    item = _flag(db, payments)
    db.commit()

    for column, value in (("signal_type", "exact_idempotency_key"),
                          ("payment_id", payments["payments"][1]),
                          ("related_payment_id", None)):
        with pytest.raises(psycopg2.errors.RaiseException):
            with _cursor(db) as cur:
                cur.execute(
                    f"UPDATE reconciliation_review_items SET {column} = %s WHERE id = %s",
                    (value, item))
        db.rollback()


def test_a_note_and_status_remain_editable(db, payments):
    """Immutability is scoped to the observation and the answer. An operator
    adding context, or a queue moving an item, is not rewriting evidence."""
    item = _flag(db, payments)
    db.commit()

    with _cursor(db) as cur:
        cur.execute(
            "UPDATE reconciliation_review_items SET status = 'reviewed', "
            "disposition = 'requires_further_review', reviewed_at = now(), "
            "reviewed_by = '7', reviewed_by_role = 'csr', "
            "disposition_note = 'asked the borrower to confirm' WHERE id = %s",
            (item,))
    db.commit()

    with _cursor(db) as cur:
        cur.execute("SELECT disposition_note FROM reconciliation_review_items "
                    "WHERE id = %s", (item,))
        assert cur.fetchone()["disposition_note"].startswith("asked the borrower")


# --------------------------------------------------------------------------
# Flagging and dispositioning move no money. Asserted, not assumed.
# --------------------------------------------------------------------------

def test_flagging_and_dispositioning_write_no_ledger_entry_and_move_no_balance(db, payments):
    with _cursor(db) as cur:
        cur.execute("SELECT balance, past_due FROM balances WHERE loan_id = %s",
                    (payments["loan_id"],))
        before = dict(cur.fetchone())
        cur.execute("SELECT count(*)::int AS n FROM ledger_entries WHERE loan_id = %s",
                    (payments["loan_id"],))
        entries_before = cur.fetchone()["n"]

    item = _flag(db, payments)
    with _cursor(db) as cur:
        cur.execute(
            "UPDATE reconciliation_review_items SET status = 'reviewed', "
            "disposition = 'confirmed_duplicate', reviewed_at = now(), "
            "reviewed_by = '7', reviewed_by_role = 'admin' WHERE id = %s", (item,))
    db.commit()

    with _cursor(db) as cur:
        cur.execute("SELECT balance, past_due FROM balances WHERE loan_id = %s",
                    (payments["loan_id"],))
        assert dict(cur.fetchone()) == before, (
            "flagging or dispositioning a review item moved the balance")
        cur.execute("SELECT count(*)::int AS n FROM ledger_entries WHERE loan_id = %s",
                    (payments["loan_id"],))
        assert cur.fetchone()["n"] == entries_before, (
            "a review disposition wrote a ledger entry -- a human classification "
            "is evidence, and any money correction needs its own authorised "
            "workflow")
        # And the payment itself is untouched: not voided, not reversed, not
        # marked invalid by the classification.
        cur.execute("SELECT auth_status, applied_at FROM payments WHERE id = %s",
                    (payments["payments"][0],))
        row = cur.fetchone()
        assert row["auth_status"] == "captured"


def test_the_table_carries_no_money_or_instrument_columns():
    """Privacy by construction, checked against the DDL rather than the docs.

    The client permitted a review item to say that one exists, which queue owns
    it, and a non-identifying reference. A reviewer reads the amount and the
    instrument from the payment inside an authenticated surface -- a review queue
    is exactly the kind of table that gets exported to a spreadsheet.
    """
    ddl = (REPO / "db" / "migrations"
           / "0045_reconciliation_review_items.sql").read_text(encoding="utf-8")
    body = ddl[ddl.index("CREATE TABLE"):ddl.index(");")]
    # Column definitions only. The comments inside the migration necessarily
    # discuss amounts, cardholder names and the source handle -- explaining why
    # they are absent is the point of them -- so a check over the raw text would
    # fail on its own documentation.
    table = "\n".join(line for line in body.splitlines()
                      if line.strip() and not line.strip().startswith("--"))

    for forbidden in ("amount", "last4", "brand", "pan", "cvv", "applicant",
                      "name", "source_ref", "processor_token"):
        assert forbidden not in table, (
            f"reconciliation_review_items has a {forbidden!r} column; the "
            f"reviewer reads that from the payment, not from the queue")
