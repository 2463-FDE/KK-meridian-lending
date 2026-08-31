"""`review_queue`'s own SQL, against real PostgreSQL and with no fake anywhere.

`test_review_queue_api.py` covers authorization and payload shape through a fake
`db.query`, and one mutation showed exactly why that is not enough on its own:
with `AND status = 'open'` deleted from the disposition UPDATE, the first version
of that fake still refused a second answer -- the FAKE was enforcing write-once,
not the statement. A fake cannot check a WHERE clause it re-implements.

What only a real database can answer:

  * the UPDATE's own condition, so a second reviewer racing the first cannot
    overwrite an answer;
  * the write-once TRIGGER underneath it, which is what makes the answer durable
    even against a caller that bypasses this module;
  * the LEFT JOIN, on a real row rather than a hand-written dict -- including
    that the projection selects no instrument column, which a fake row can only
    fail to contain;
  * that a `confirmed_duplicate` answer leaves the balance and the ledger where
    they were. The client's wording is that a flag is never permission to move
    money, and the only proof of that is money that did not move.
"""
import os
import pathlib
from decimal import Decimal

import psycopg2
import psycopg2.extras
import pytest

from app import review_queue

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against"
)

REPO = pathlib.Path(__file__).resolve().parents[3]
INIT = REPO / "db" / "init"
SCHEMA = "servicing_review_queue_test"
INIT_FILES = ("001_schema.sql", "004_decision_events.sql", "005_manual_reviews.sql",
              "006_decision_attempts.sql", "007_ledger_opening_balances.sql")


class _Actor:
    """The shape `principal.Principal` presents to this module: a subject and a
    role. Not the real class, because verifying an Ed25519 assertion is
    `test_review_queue_api.py`'s subject and irrelevant to the SQL."""

    def __init__(self, subject="7", role="csr"):
        self.subject = subject
        self.role = role


@pytest.fixture
def pg(monkeypatch):
    """A throwaway schema, with `review_queue.db.query` pointed at it.

    The module's real SQL runs -- only the connection is redirected, which is the
    same arrangement `test_offer_repair_real_postgres.py` uses. A `search_path`
    set once per connection is what keeps the DDL and every statement in the same
    schema.
    """
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS %s CASCADE" % SCHEMA)
        cur.execute("CREATE SCHEMA %s" % SCHEMA)
        cur.execute("SET search_path TO %s" % SCHEMA)
        for name in INIT_FILES:
            cur.execute((INIT / name).read_text(encoding="utf-8"))

    def _query(sql, params=None):
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SET search_path TO %s" % SCHEMA)
            cur.execute(sql, params or ())
            return cur.fetchall() if cur.description else []

    monkeypatch.setattr(review_queue.db, "query", _query)
    yield _query
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS %s CASCADE" % SCHEMA)
    conn.close()


@pytest.fixture
def flagged(pg):
    """One loan, two captured payments 18 minutes apart, one review item.

    The payments carry `last4` and `brand` deliberately: the guard that says the
    queue exposes no instrument data is worth nothing against rows where the
    columns are empty.
    """
    pg("INSERT INTO applicants (name) VALUES ('Test Borrower')")
    app_id = pg("INSERT INTO applications (amount, term_months, status) "
                "VALUES (5000, 24, 'funded') RETURNING id")[0]["id"]
    loan_id = pg("INSERT INTO loans (app_id, applicant_name, principal, "
                 "note_rate_pct, term_months, status) VALUES "
                 "(%s, 'Test Borrower', 5000.00, 7.990, 24, 'current') RETURNING id",
                 (app_id,))[0]["id"]
    pg("INSERT INTO balances (loan_id, balance, past_due) VALUES (%s, 4750.00, 0.00)",
       (loan_id,))

    ids = []
    for key, minutes in (("idem-earlier", 18), ("idem-later", 0)):
        ids.append(pg(
            "INSERT INTO payments (loan_id, amount, method, last4, brand, "
            "idempotency_key, auth_status, captured_at, processor_ref, "
            "source_ref, capture_source) VALUES "
            "(%s, 250.00, 'card', '4242', 'visa', %s, 'captured', "
            "now() - make_interval(mins => %s), %s, 'src_mock_x', 'processor') "
            "RETURNING id",
            (loan_id, key, minutes, "PR-%s" % key))[0]["id"])

    item_id = pg(
        "INSERT INTO reconciliation_review_items (signal_type, payment_id, "
        "related_payment_id, loan_id, correlation_ref) VALUES "
        "('heuristic_30_minute_candidate', %s, %s, %s, 'pay_corr') RETURNING id",
        (ids[1], ids[0], loan_id))[0]["id"]

    return {"loan_id": loan_id, "payments": ids, "item_id": item_id}


# --- reading -------------------------------------------------------------------


def test_the_queue_joins_both_payments_from_real_rows(flagged):
    items = review_queue.queue()

    assert len(items) == 1
    item = items[0]
    assert item["payment"]["id"] == flagged["payments"][1]
    assert item["related_payment"]["id"] == flagged["payments"][0]
    assert item["payment"]["amount"] == "250.00"
    assert item["related_payment"]["captured_at"] < item["payment"]["captured_at"]


def test_the_projection_selects_no_instrument_column(flagged):
    """The payments really do carry `4242`/`visa`. A fake row without those
    columns proves nothing about a SELECT that would have returned them."""
    flat = repr(review_queue.queue())

    for leaked in ("4242", "visa", "PR-idem", "idem-later", "src_mock"):
        assert leaked not in flat, (
            "the review queue returned %r from the payments row" % leaked)


def test_an_item_with_no_related_payment_survives_the_join(pg, flagged):
    """A provider-reference collision may not know which earlier capture holds
    the reference, so `related_payment_id` is nullable -- and a LEFT JOIN is what
    keeps the item visible instead of dropping it out of the queue entirely. An
    INNER JOIN here would have hidden precisely the strongest signal.

    Inserted rather than UPDATEd: the write-once trigger refuses to change an
    item's subject at all, which is the first thing this test found. That is the
    trigger working -- the signal and the payment it is about are immutable -- so
    the case is built as a new item on the other payment instead.
    """
    lonely = pg(
        "INSERT INTO reconciliation_review_items (signal_type, payment_id, "
        "related_payment_id, loan_id) VALUES "
        "('exact_provider_transaction_id', %s, NULL, %s) RETURNING id",
        (flagged["payments"][0], flagged["loan_id"]))[0]["id"]

    items = {item["id"]: item for item in review_queue.queue()}

    assert lonely in items, "an item with no related payment vanished from the queue"
    assert items[lonely]["related_payment"] is None
    # And the one that does have a related payment still reports it, so the LEFT
    # JOIN did not simply stop joining.
    assert items[flagged["item_id"]]["related_payment"] is not None


def test_a_reviewed_item_leaves_the_open_queue(flagged):
    review_queue.record_disposition(flagged["item_id"],
                                    disposition="legitimate_distinct_payment",
                                    note=None, actor=_Actor())

    assert review_queue.queue(status="open") == []
    assert len(review_queue.queue(status="reviewed")) == 1


def test_the_counts_split_open_by_signal_category(pg, flagged):
    pg("INSERT INTO reconciliation_review_items (signal_type, payment_id, loan_id) "
       "VALUES ('exact_idempotency_key', %s, %s)",
       (flagged["payments"][0], flagged["loan_id"]))

    assert review_queue.counts() == {"open_exact": 1, "open_heuristic": 1,
                                     "reviewed": 0}


# --- answering -----------------------------------------------------------------


def test_a_disposition_is_recorded_with_its_reviewer(flagged):
    item = review_queue.record_disposition(
        flagged["item_id"], disposition="confirmed_duplicate",
        note="same charge twice", actor=_Actor(subject="11", role="admin"))

    assert item["status"] == "reviewed"
    assert item["disposition"] == "confirmed_duplicate"
    assert item["reviewed_by"] == "11" and item["reviewed_by_role"] == "admin"
    assert item["reviewed_at"] is not None
    assert item["disposition_note"] == "same charge twice"


def test_a_second_answer_is_refused_by_the_statements_own_condition(flagged):
    """The race, not the sequence. `WHERE ... AND status = 'open'` is what makes
    the second UPDATE match no row -- and with that clause removed, this test is
    what fails, on a real database rather than against a fake's re-implementation
    of it."""
    review_queue.record_disposition(flagged["item_id"],
                                    disposition="confirmed_duplicate",
                                    note=None, actor=_Actor(subject="7"))

    with pytest.raises(review_queue.ReviewConflict) as err:
        review_queue.record_disposition(
            flagged["item_id"], disposition="legitimate_distinct_payment",
            note="disagree", actor=_Actor(subject="9", role="admin"))

    assert "write-once" in str(err.value)
    still = review_queue.get(flagged["item_id"])
    assert still["disposition"] == "confirmed_duplicate"
    assert still["reviewed_by"] == "7", "the second reviewer overwrote the first"


def test_the_trigger_refuses_a_rewrite_that_bypasses_this_module(pg, flagged):
    """Belt and braces, and the braces matter: the UPDATE's condition protects
    callers who come through here, and the trigger protects the row from everyone
    else -- an ops script, a migration, a future endpoint."""
    review_queue.record_disposition(flagged["item_id"],
                                    disposition="confirmed_duplicate",
                                    note=None, actor=_Actor())

    with pytest.raises(psycopg2.errors.RaiseException) as err:
        pg("UPDATE reconciliation_review_items SET disposition = "
           "'legitimate_distinct_payment' WHERE id = %s", (flagged["item_id"],))

    assert "write-once" in str(err.value)


def test_a_missing_item_is_a_conflict(pg):
    with pytest.raises(review_queue.ReviewConflict) as err:
        review_queue.record_disposition(999_999, disposition="confirmed_duplicate",
                                        note=None, actor=_Actor())

    assert "does not exist" in str(err.value)


def test_an_unauthorised_disposition_never_reaches_the_database(flagged):
    """The database CHECK would refuse a fourth disposition anyway; this refuses
    it before the statement, so the caller gets a named list instead of a
    constraint violation."""
    with pytest.raises(ValueError) as err:
        review_queue.record_disposition(flagged["item_id"],
                                        disposition="reverse_it",
                                        note=None, actor=_Actor())

    assert "confirmed_duplicate" in str(err.value)
    assert review_queue.get(flagged["item_id"])["status"] == "open"


# --- and nothing moved ---------------------------------------------------------


def test_confirming_a_duplicate_moves_no_money(pg, flagged):
    """The answer most likely to be mistaken for an instruction.

    The client's wording is that a flag is never a duplicate conclusion and never
    permission to move money. `confirmed_duplicate` IS the conclusion -- and it
    still moves nothing.

    Correcting the loan balance afterwards is a maker-checker ADJUSTMENT proposed
    by one person and approved by another. It is not a card reversal, and this
    docstring used to describe one: `maker_checker.ENTRY_TYPES` is
    `{adjustment, fee_waived}`, and no service here exposes a refund, void,
    reversal or chargeback route.
    """
    before_balance = pg("SELECT balance, past_due FROM balances WHERE loan_id = %s",
                        (flagged["loan_id"],))[0]
    before_ledger = pg("SELECT count(*) AS n FROM ledger_entries")[0]["n"]
    before_payment = pg("SELECT auth_status, applied_at, amount FROM payments "
                        "WHERE id = %s", (flagged["payments"][1],))[0]

    review_queue.record_disposition(flagged["item_id"],
                                    disposition="confirmed_duplicate",
                                    note="reverse this one", actor=_Actor())

    after_balance = pg("SELECT balance, past_due FROM balances WHERE loan_id = %s",
                       (flagged["loan_id"],))[0]
    assert after_balance["balance"] == before_balance["balance"] == Decimal("4750.00")
    assert after_balance["past_due"] == before_balance["past_due"]
    assert pg("SELECT count(*) AS n FROM ledger_entries")[0]["n"] == before_ledger
    after_payment = pg("SELECT auth_status, applied_at, amount FROM payments "
                       "WHERE id = %s", (flagged["payments"][1],))[0]
    assert after_payment == before_payment, (
        "recording a review answer changed the payment it was about")


def test_no_pending_movement_is_raised_either(pg, flagged):
    """Not even the safe version of acting on it. A proposal queued automatically
    would put a money movement one click from happening on the strength of a flag
    the client said is not a conclusion.

    The movement in question is a balance adjustment, not a card reversal --
    Meridian has no reversal capability.
    """
    review_queue.record_disposition(flagged["item_id"],
                                    disposition="confirmed_duplicate",
                                    note=None, actor=_Actor())

    assert pg("SELECT count(*) AS n FROM pending_movements")[0]["n"] == 0
