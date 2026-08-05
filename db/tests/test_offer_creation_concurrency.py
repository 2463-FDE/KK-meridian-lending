"""Real, multi-connection proof for the offer-creation idempotency fix
(borrower-workflow audit, follow-up to test_accept_token_lifecycle.py).

Bug: the public /apply page's own "view your offer" step always tried to
CREATE an offer via origination-service's POST /offer -- which used to
reject with a 409 the instant ANY offer already existed, including the
completely normal case where run_decision's/review_application's own
best-effort auto-generation (disclosure_graph.auto_generate_offer) had
already created one moments earlier. The borrower could never reach the
offer/accept step through the browser.

Root cause was NOT a missing concurrency guard -- disclosure-service's own
INSERT ... ON CONFLICT (decision_id) DO NOTHING + read-back fallback
(services/disclosure-service/app/routers/offers.py::create_offer) was
already a real, database-enforced "exactly one offer per decision"
guarantee (offers.decision_id / offers.app_id are both UNIQUE, migrations
0009/0011). The bug was origination-service's OWN redundant pre-check
(routers/offers.py::make_offer) treating "an offer already exists" as
always an error. This file proves the underlying database mechanism this
fix now relies on is genuinely safe under real concurrent access -- two
independent Postgres connections, synchronized with a threading.Barrier,
replicating disclosure-service's exact INSERT/SELECT statement sequence.
"""
import os
import threading

import psycopg2
import psycopg2.extras
import pytest

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against")

SCHEMA = "concurrency_test_offer_creation"


@pytest.fixture
def setup_conn():
    connection = psycopg2.connect(DATABASE_URL)
    connection.autocommit = False
    cur = connection.cursor()
    cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    cur.execute(f"CREATE SCHEMA {SCHEMA}")
    connection.commit()
    with connection.cursor() as c:
        c.execute(f"""
            SET search_path TO {SCHEMA};
            CREATE TABLE applications (id SERIAL PRIMARY KEY, amount NUMERIC(14,2) DEFAULT 9000, term_months INTEGER DEFAULT 24);
            CREATE TABLE decisions (app_id INTEGER PRIMARY KEY REFERENCES applications(id), outcome TEXT NOT NULL);
            CREATE TABLE offers (
                id SERIAL PRIMARY KEY,
                app_id INTEGER NOT NULL UNIQUE REFERENCES applications(id),
                decision_id INTEGER NOT NULL UNIQUE REFERENCES decisions(app_id),
                fee_pct_used NUMERIC(5,4) NOT NULL,
                apr NUMERIC(7,3) NOT NULL,
                finance_charge NUMERIC(14,2) NOT NULL,
                monthly_payment NUMERIC(14,2) NOT NULL,
                amount_financed NUMERIC(14,2) NOT NULL,
                total_of_payments NUMERIC(14,2) NOT NULL
            );
        """)
    connection.commit()
    yield connection
    cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    connection.commit()
    connection.close()


def _seed(conn, app_id, outcome="approve"):
    with conn.cursor() as c:
        c.execute(f"SET search_path TO {SCHEMA}")
        c.execute("INSERT INTO applications (id) VALUES (%s)", (app_id,))
        c.execute("INSERT INTO decisions (app_id, outcome) VALUES (%s, %s)", (app_id, outcome))
    conn.commit()


def _create_offer_attempt(app_id, barrier=None):
    """Independent connection -- replicates disclosure-service's exact
    create_offer statement sequence (INSERT ... ON CONFLICT (decision_id)
    DO NOTHING, then read back on conflict). Returns (offer_id, created)."""
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
            c.execute(f"SET search_path TO {SCHEMA}")
            if barrier is not None:
                barrier.wait()
            # Mirrors disclosure-service's own fix: offers.decision_id and
            # offers.app_id are two separate UNIQUE constraints (always
            # equal in value here) -- a genuinely concurrent insert can hit
            # either one, so a UniqueViolation on the constraint ON
            # CONFLICT doesn't target is caught and treated the same as
            # DO NOTHING firing.
            try:
                c.execute(
                    "INSERT INTO offers (app_id, decision_id, fee_pct_used, apr, finance_charge, "
                    "monthly_payment, amount_financed, total_of_payments) "
                    "SELECT d.app_id, d.app_id, 0.03, 7.99, 500.0, 400.0, 8700.0, 9600.0 "
                    "FROM decisions d WHERE d.app_id = %s AND d.outcome = 'approve' "
                    "ON CONFLICT (decision_id) DO NOTHING "
                    "RETURNING id",
                    (app_id,),
                )
                row = c.fetchone()
            except psycopg2.errors.UniqueViolation:
                # Plain SET (session-scope, not SET LOCAL) is still undone
                # by ROLLBACK if it ran inside the transaction being rolled
                # back -- re-apply it or the read-back below silently
                # queries the wrong (default) schema and finds nothing.
                conn.rollback()
                c.execute(f"SET search_path TO {SCHEMA}")
                row = None
            if row:
                conn.commit()
                return row["id"], True
            c.execute("SELECT id FROM offers WHERE decision_id = %s", (app_id,))
            existing = c.fetchone()
            conn.commit()
            return (existing["id"] if existing else None), False
    finally:
        conn.close()


def test_two_simultaneous_offer_creation_requests_produce_exactly_one_offer(setup_conn):
    """Two independent connections both attempting to create the SAME
    application's offer at the same instant -- exactly one 'created', one
    'already existed', both referencing the identical row, no duplicate."""
    _seed(setup_conn, 1)

    barrier = threading.Barrier(2)
    results = {}

    def _run(key):
        results[key] = _create_offer_attempt(1, barrier=barrier)

    t1 = threading.Thread(target=_run, args=("a",))
    t2 = threading.Thread(target=_run, args=("b",))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    ids = {results["a"][0], results["b"][0]}
    created_flags = sorted([results["a"][1], results["b"][1]])
    assert len(ids) == 1, f"both attempts must resolve to the SAME offer row: {results}"
    assert created_flags == [False, True], f"exactly one must report created=True: {results}"

    with setup_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        c.execute(f"SET search_path TO {SCHEMA}")
        c.execute("SELECT count(*) AS n FROM offers WHERE app_id = 1")
        assert c.fetchone()["n"] == 1


def test_automatic_generation_and_browser_request_racing_produce_exactly_one_offer(setup_conn):
    """Same underlying mechanism as the test above, framed as the real
    scenario this fix targets: run_decision's best-effort auto-generation
    and the borrower's own browser reaching the offer step at nearly the
    same instant. No special-casing exists (or is needed) for 'automatic'
    vs 'browser' callers -- both go through the identical idempotent path,
    which is exactly why this fix requires no new architecture."""
    _seed(setup_conn, 2)

    barrier = threading.Barrier(2)
    results = {}

    def _run(key):
        results[key] = _create_offer_attempt(2, barrier=barrier)

    auto_gen = threading.Thread(target=_run, args=("auto_generation",))
    browser = threading.Thread(target=_run, args=("browser_request",))
    auto_gen.start()
    browser.start()
    auto_gen.join(timeout=10)
    browser.join(timeout=10)

    ids = {results["auto_generation"][0], results["browser_request"][0]}
    assert len(ids) == 1, results
    assert sorted([results["auto_generation"][1], results["browser_request"][1]]) == [False, True]


def test_retry_after_timeout_returns_the_same_offer(setup_conn):
    """A client that creates an offer, then (believing the first call
    timed out, or simply re-entering the offer step) retries, must get
    back the IDENTICAL offer -- never a second row, never a 409."""
    _seed(setup_conn, 3)

    first_id, first_created = _create_offer_attempt(3)
    second_id, second_created = _create_offer_attempt(3)

    assert first_created is True
    assert second_created is False
    assert first_id == second_id

    with setup_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        c.execute(f"SET search_path TO {SCHEMA}")
        c.execute("SELECT count(*) AS n FROM offers WHERE app_id = 3")
        assert c.fetchone()["n"] == 1


def test_no_offer_created_for_a_denied_application(setup_conn):
    """The INSERT's own WHERE d.outcome = 'approve' is the real, atomic
    gate -- a denied application's create attempt must insert nothing and
    the read-back fallback must find nothing either (a 422 upstream, not a
    fabricated offer)."""
    _seed(setup_conn, 4, outcome="deny")

    # No decisions row satisfies outcome='approve', so the INSERT's own
    # SELECT yields zero rows and ON CONFLICT never fires; the read-back
    # SELECT also finds nothing -- mirrors disclosure-service's own 422
    # branch (raised there, not by this raw-SQL replica).
    offer_id, created = _create_offer_attempt(4)
    assert offer_id is None
    assert created is False

    with setup_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        c.execute(f"SET search_path TO {SCHEMA}")
        c.execute("SELECT count(*) AS n FROM offers WHERE app_id = 4")
        assert c.fetchone()["n"] == 0
