"""Payment history distinguishes the reasons an allocation is absent.

The client's decision: payment history is the durable source of truth, an applied
payment shows its REAL allocation, and a captured-but-unapplied one says exactly
"Captured -- allocation pending". Never an estimate.

The allocation half was already right -- `_allocations_by_payment` reads the
ledger entries that moved the balance, and returns `null` rather than `0.00` when
there is no evidence. What was missing is WHY the evidence is missing. Three
different situations produce no ledger entries:

    captured, not applied yet -> the payment is in flight
    applied, no entries       -> a legacy row from before the ledger existed
    failed                    -> declined; nothing was ever applied

All three came back identically, so history told a borrower whose payment had
been captured seconds earlier that the allocation was "not available for this
historical payment" -- wrong about the payment and wrong about the reason.

`auth_status` and `applied` now travel with the row. The vocabulary is the one
`PaymentOut.status` already uses, so the receipt and the history row describe one
payment the same way instead of inventing a second set of words.

Against real PostgreSQL: the point is what the ledger and the payment columns
actually hold together, and a mock would let this file agree with itself.
"""
import os

import pytest

pytest.importorskip("sqlalchemy")

DATABASE_URL = os.getenv("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against")


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient
    from app.main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture()
def conn():
    import psycopg2
    import psycopg2.extras
    c = psycopg2.connect(DATABASE_URL)
    c.autocommit = False
    yield c
    c.rollback()
    c.close()


def _a_loan(cur):
    cur.execute(
        "INSERT INTO loans (applicant_name, principal, note_rate_pct, term_months, "
        "                   regular_payment, regular_payment_count, final_payment, "
        "                   schedule_version, status) "
        "VALUES ('History Reason Fixture', 9000.00, 7.99, 36, 281.96, 35, 281.90, "
        "        'B1', 'current') RETURNING id")
    loan_id = cur.fetchone()["id"]
    cur.execute("INSERT INTO balances (loan_id, balance, past_due) "
                "VALUES (%s, 9000.00, 0.00)", (loan_id,))
    return loan_id


def _a_payment(cur, loan_id, *, amount, auth_status, applied):
    cur.execute(
        "INSERT INTO payments (loan_id, amount, method, last4, brand, auth_status, "
        "                      idempotency_key, applied_at) "
        "VALUES (%s, %s, 'card', '1111', 'Visa', %s, %s, "
        "        CASE WHEN %s THEN now() ELSE NULL END) RETURNING id",
        (loan_id, amount, auth_status, f"hist-{loan_id}-{amount}-{auth_status}", applied))
    return cur.fetchone()["id"]


def _row_for(client, loan_id, payment_id):
    resp = client.get(f"/loans/{loan_id}/payments",
                      headers={"X-Internal-Token": os.getenv("INTERNAL_SERVICE_TOKEN", "")})
    assert resp.status_code == 200, resp.text
    for item in resp.json()["items"]:
        if item["id"] == payment_id:
            return item
    raise AssertionError(f"payment {payment_id} missing from history")


def test_a_captured_but_unapplied_payment_is_reported_as_such(client, conn):
    """The state the client's decision names, and the one that was indistinguishable."""
    import psycopg2.extras
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        loan = _a_loan(cur)
        pid = _a_payment(cur, loan, amount=125.00, auth_status="captured", applied=False)
    conn.commit()

    row = _row_for(client, loan, pid)

    assert row["auth_status"] == "captured"
    assert row["applied"] is False
    # No allocation, and no estimate standing in for one.
    assert row["applied_to_fees"] is None
    assert row["applied_to_interest"] is None
    assert row["applied_to_principal"] is None


def test_a_declined_payment_is_reported_as_declined(client, conn):
    """Nothing was applied, and that must not read as a figure that failed to load."""
    import psycopg2.extras
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        loan = _a_loan(cur)
        pid = _a_payment(cur, loan, amount=70.00, auth_status="failed", applied=False)
    conn.commit()

    row = _row_for(client, loan, pid)

    assert row["auth_status"] == "failed"
    assert row["applied"] is False
    assert row["applied_to_principal"] is None


def test_an_applied_payment_reports_the_ledger_allocation_and_says_it_is_applied(
        client, conn):
    """The durable answer: the figures come from the entries that moved the balance."""
    import psycopg2.extras
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        loan = _a_loan(cur)
        pid = _a_payment(cur, loan, amount=100.00, auth_status="captured", applied=True)
        # The ledger is what an allocation is read from, so the evidence is
        # written the way the apply path writes it: one row per component moved.
        for component, amount in (("fees", -10.00), ("interest", -15.00),
                                  ("principal", -75.00)):
            cur.execute(
                "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, "
                "                            payment_id, reason) "
                "VALUES (%s, %s, %s, 'payment', %s, 'history reason fixture')",
                (loan, component, amount, pid))
    conn.commit()

    row = _row_for(client, loan, pid)

    assert row["applied"] is True
    assert row["applied_to_fees"] == 10.00
    assert row["applied_to_interest"] == 15.00
    assert row["applied_to_principal"] == 75.00
    total = (row["applied_to_fees"] + row["applied_to_interest"]
             + row["applied_to_principal"])
    assert total == pytest.approx(row["amount"], abs=0.005), (
        "the components must foot to the payment; a borrower reading a receipt "
        "adds them up")


def test_a_legacy_applied_payment_with_no_entries_stays_unknown(client, conn):
    """Applied before the ledger existed: genuinely unknown, and still said so.

    This is the case the "not available for this historical payment" wording was
    written for, and it must keep that wording rather than being absorbed into
    the new pending state.
    """
    import psycopg2.extras
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        loan = _a_loan(cur)
        pid = _a_payment(cur, loan, amount=200.00, auth_status="captured", applied=True)
    conn.commit()

    row = _row_for(client, loan, pid)

    assert row["applied"] is True, "the row is applied..."
    assert row["applied_to_principal"] is None, "...but carries no ledger evidence"


def test_ledger_evidence_decides_regardless_of_the_status_columns(client, conn):
    """Entries win. The status is only ever used to explain an ABSENCE.

    A row whose columns disagree with the ledger must still report what the
    ledger holds -- the entries are what moved the balance, and letting a status
    column suppress them would be the browser's estimate problem moved server-side.
    """
    import psycopg2.extras
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        loan = _a_loan(cur)
        # applied_at deliberately NULL while entries exist.
        pid = _a_payment(cur, loan, amount=50.00, auth_status="captured", applied=False)
        cur.execute(
            "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, "
            "                            payment_id, reason) "
            "VALUES (%s, 'principal', -50.00, 'payment', %s, 'disagreement fixture')",
            (loan, pid))
    conn.commit()

    row = _row_for(client, loan, pid)

    assert row["applied_to_principal"] == 50.00, (
        "the ledger holds an entry for this payment, so the allocation must be "
        "reported whatever the status columns say")
