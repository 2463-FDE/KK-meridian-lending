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

**HOW IT GETS THAT POSTGRES, which is where the first version was wrong.** It
connected to `DATABASE_URL` and inserted straight into `loans`, assuming the
production schema was already there. It is, on a developer machine running the
stack; it is not in CI, where the `backend (servicing-service)` job starts a bare
Postgres service container. All five cases failed with
`psycopg2.errors.UndefinedTable: relation "loans" does not exist` -- a test
harness defect that pointed at the database rather than at the behaviour, which
is exactly the shape RF-26 records.

So it builds its own throwaway schema from the CANONICAL definitions in
`db/init`, via `db/tests/real_schema.py` (the RF-26 helper, PR #159). Not a
hand-written `loans`: a hand-written copy is how the drift RF-26 measured
happened -- one of the three copies it found created five columns out of
twenty-four -- and this file depends on real column shapes, `payments.auth_status`
and `payments.applied_at` among them. Taking the definitions verbatim means the
next column added to `payments` arrives here by construction.

Only the tables the route actually touches are built, plus `balances`.
`loan_payments` reads `payments` and `ledger_entries` and never looks at
`balances` -- but a `loans` row without one models a database production cannot
have, and the equivalent servicing harness makes the same call for the same
reason.

The app is pointed at that schema by overriding the ONE dependency the route
resolves its session through, so the code under test is the shipped route rather
than a copy of its query.
"""
import importlib.util
import os
import pathlib

import pytest

pytest.importorskip("sqlalchemy")

#: `db/tests/real_schema.py`, loaded by path -- standard library only, so it
#: imports cleanly from a service test that is not on the repo's sys.path.
_REAL_SCHEMA_PATH = (pathlib.Path(__file__).resolve().parents[3]
                     / "db" / "tests" / "real_schema.py")
assert _REAL_SCHEMA_PATH.is_file(), (
    "expected the canonical schema helper at %s -- if it moved, this test must "
    "fail rather than fall back to a hand-written table" % _REAL_SCHEMA_PATH)
_spec = importlib.util.spec_from_file_location("meridian_real_schema",
                                               _REAL_SCHEMA_PATH)
real_schema = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(real_schema)

DATABASE_URL = os.getenv("DATABASE_URL")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against")

SCHEMA = "servicing_history_reason_test"

#: Everything the route reads, plus `balances` -- see the module docstring.
TABLES = ["loans", "balances", "payments", "ledger_entries"]


@pytest.fixture()
def pg():
    """A throwaway schema holding the real shapes, dropped afterwards."""
    import psycopg2
    admin = psycopg2.connect(DATABASE_URL)
    admin.autocommit = True
    with admin.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(real_schema.sql_for(SCHEMA, TABLES))
        # Guard the guard: a helper that silently produced nothing would leave
        # every case below passing against an empty schema for the wrong reason.
        cur.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_schema = %s",
            (SCHEMA,))
        built = cur.fetchone()[0]
    assert built == len(TABLES), (
        f"expected {len(TABLES)} canonical tables in {SCHEMA}, built {built}")
    yield admin
    with admin.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    admin.close()


@pytest.fixture()
def client(pg):
    """The shipped app, with its session dependency pointed at the schema above.

    `loan_payments` takes `session: Session = Depends(get_session)`, so
    overriding that one dependency reaches the real route, the real query and the
    real response model. Nothing about the code under test is replaced.

    `TestClient(app)` is used WITHOUT its context manager on purpose: the
    lifespan starts the reconciliation scheduler, which uses the raw psycopg2
    layer on the default search_path and would log `UndefinedTable` noise this
    file has no interest in. No startup work is needed to serve this route.
    """
    from fastapi.testclient import TestClient
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from app.database import get_session
    from app.main import app

    engine = create_engine(
        DATABASE_URL, future=True, pool_pre_ping=True,
        connect_args={"options": f"-csearch_path={SCHEMA}"})
    Session = sessionmaker(bind=engine, autoflush=False, future=True)

    def _session_in_the_test_schema():
        session = Session()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_session] = _session_in_the_test_schema
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_session, None)
        engine.dispose()


@pytest.fixture()
def conn(pg):
    """A writer on the same schema. Committed rows are what the route then reads."""
    import psycopg2
    import psycopg2.extras
    c = psycopg2.connect(DATABASE_URL)
    c.autocommit = False
    with c.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
    yield c
    c.rollback()
    c.close()


def _a_loan(cur):
    cur.execute(
        "INSERT INTO loans (applicant_name, principal, note_rate_pct, term_months, "
        "                   regular_payment, regular_payment_count, final_payment, "
        "                   schedule_version, status) "
        "VALUES ('History Reason Fixture', 9000.00, 7.99, 36, 281.99, 35, 281.85, "
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


def test_a_payment_awaiting_authorization_is_reported_as_pending_auth(client, conn):
    """The fourth absence reason, and the one the first version missed.

    Codex ALLOC-PENDING-AUTH-001. `payment-service` inserts the row as
    `auth_status = 'pending'` BEFORE it calls the processor, so a row in that
    state means authorization is in flight -- or was left in flight by a crash
    mid-authorization. Nothing has been captured.

    The API's job here is to carry the value truthfully; the browser decides the
    wording, and it must not be the "Captured" sentence (which would assert a
    charge that may never have happened) nor the historical one (nothing about
    this payment is old). This asserts the API hands over what that decision
    needs.
    """
    import psycopg2.extras
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        loan = _a_loan(cur)
        pid = _a_payment(cur, loan, amount=310.00, auth_status="pending", applied=False)
    conn.commit()

    row = _row_for(client, loan, pid)

    assert row["auth_status"] == "pending", (
        "history flattened an unconfirmed authorization into some other status, "
        "so the browser cannot tell it from a captured payment")
    assert row["applied"] is False
    assert row["applied_to_fees"] is None
    assert row["applied_to_interest"] is None
    assert row["applied_to_principal"] is None


def test_history_carries_every_auth_status_the_payment_table_can_hold(client, conn):
    """Guard the guard, derived rather than listed.

    The four cases above each name one status. This one reads the CHECK
    constraint -- or the values `payment-service` writes -- and asserts history
    round-trips each, so a status added later shows up here instead of silently
    falling into whatever branch the frontend has last.
    """
    import psycopg2.extras
    statuses = ("captured", "pending", "failed")
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        loan = _a_loan(cur)
        ids = {s: _a_payment(cur, loan, amount=10.00 + i, auth_status=s, applied=False)
               for i, s in enumerate(statuses)}
    conn.commit()

    for status, pid in ids.items():
        row = _row_for(client, loan, pid)
        assert row["auth_status"] == status, (
            "history reported %r for a payment stored as %r"
            % (row["auth_status"], status))
