"""PR #6 review, Finding 2 -- two invariants that a mocked-cursor test
cannot prove, requiring real PostgreSQL:

  1. Two concurrent reruns for the same application result in exactly ONE
     external call to decision-service, never two -- proven with real row
     locking (SELECT ... FOR UPDATE) across genuinely independent
     connections/threads, not a single in-process fake cursor standing in
     for "the database."
  2. A failure partway through TXN B (after at least one write has already
     executed) rolls back EVERYTHING in that transaction together --
     decisions, decision_events, the attempt's completion, and the
     accept-token mint -- proven by forcing a real constraint violation
     (decision_events.model_version NOT NULL) via a deliberately malformed
     decision-service response, not by changing any production code.

Both tests point the REAL app.db module at a throwaway Postgres schema
(dropped/recreated per test) instead of the mocked db.transaction()/
db.query() every other test in this directory uses -- only the HTTP
boundary to decision-service (app.clients.post) is mocked, exactly what
"mocking only the external HTTP boundary is acceptable" calls for.
"""
import os
import threading
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import clients, db, decision_state, disclosure_graph
from app.main import app

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent.parent
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"

SCHEMA = "origination_real_pg_test"
client = TestClient(app)


def _full_schema_sql():
    return f"""
        SET search_path TO {SCHEMA};
        CREATE TABLE applicants (
            id SERIAL PRIMARY KEY,
            name TEXT NOT NULL,
            ssn TEXT
        );
        CREATE TABLE applications (
            id SERIAL PRIMARY KEY,
            applicant_id INTEGER REFERENCES applicants(id),
            amount NUMERIC(14,2) NOT NULL,
            term_months INTEGER NOT NULL,
            income NUMERIC(14,2),
            access_token TEXT,
            status TEXT DEFAULT 'submitted',
            accept_token_hash TEXT,
            accept_token_expires_at TIMESTAMPTZ,
            accept_token_consumed_at TIMESTAMPTZ
        );
        CREATE TABLE decisions (
            app_id INTEGER PRIMARY KEY REFERENCES applications(id),
            outcome TEXT NOT NULL
        );
        CREATE TABLE manual_reviews (
            id SERIAL PRIMARY KEY,
            app_id INTEGER NOT NULL REFERENCES applications(id) UNIQUE,
            reviewer_role TEXT NOT NULL,
            reviewer_name TEXT,
            outcome TEXT NOT NULL,
            reason TEXT NOT NULL,
            reviewed_at TIMESTAMPTZ NOT NULL DEFAULT now()
        );
        CREATE TABLE decision_events (
            id SERIAL PRIMARY KEY,
            app_id INTEGER NOT NULL REFERENCES applications(id),
            requested_amount NUMERIC(14,2),
            term_months INTEGER,
            annual_income NUMERIC(14,2),
            bureau_score INTEGER,
            model_score DOUBLE PRECISION,
            model_version TEXT NOT NULL,
            top_features JSONB,
            decision TEXT NOT NULL,
            reason_codes JSONB NOT NULL DEFAULT '[]'::jsonb,
            attempt_id INTEGER
        );
    """


@pytest.fixture
def real_db(monkeypatch):
    """Points app.db at a real, throwaway Postgres schema instead of the
    mocked db.transaction()/db.query() every other origination-service test
    uses. db.transaction() opens a fresh connection per call and db.query()/
    db.get_conn() cache one shared connection -- both read the module-level
    DATABASE_URL/`_conn` at call time, so monkeypatching them here redirects
    every real call the application code makes for the duration of the test,
    with no production code changes."""
    setup_conn = psycopg2.connect(DATABASE_URL)
    setup_conn.autocommit = False
    with setup_conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
    setup_conn.commit()
    with setup_conn.cursor() as cur:
        cur.execute(_full_schema_sql())
    setup_conn.commit()
    with setup_conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute((MIGRATIONS_DIR / "0023_decision_attempts.sql").read_text())
    setup_conn.commit()

    schema_url = DATABASE_URL + ("&" if "?" in DATABASE_URL else "?") + f"options=-csearch_path%3D{SCHEMA}"
    monkeypatch.setattr(db, "DATABASE_URL", schema_url)
    monkeypatch.setattr(db, "_conn", None)  # force get_conn() to open a fresh connection under the new URL

    yield setup_conn

    with setup_conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    setup_conn.commit()
    setup_conn.close()


def _seed_application(conn, app_id, amount=9000, term_months=24, income=40000,
                       access_token="real-access-token", status="submitted"):
    with conn.cursor() as c:
        c.execute(f"SET search_path TO {SCHEMA}")
        c.execute(
            "INSERT INTO applicants (id, name, ssn) VALUES (%s, %s, %s)",
            (app_id, "Jane Borrower", "123456781"),
        )
        c.execute(
            "INSERT INTO applications (id, applicant_id, amount, term_months, income, access_token, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s)",
            (app_id, app_id, amount, term_months, income, access_token, status),
        )
    conn.commit()


# --- Test 1: exactly one external decision-service call under real concurrency ---

def test_two_concurrent_reruns_result_in_exactly_one_bureau_call(real_db, monkeypatch):
    """Coordination: two genuine Python threads, each with its OWN psycopg2
    connection (via the real, monkeypatched db.transaction()), racing
    decision_state.start_decision_attempt -- the actual function
    run_decision calls, against real Postgres row locking. A
    threading.Event (not a timing guess) holds the winner's mocked
    "external call" open until the loser has already made and lost its own
    attempt, so the loser is proven to race a genuinely still-'in_progress'
    attempt, not an already-completed one."""
    app_id = 501
    _seed_application(real_db, app_id)

    call_started = threading.Event()
    release_call = threading.Event()
    post_calls = []
    calls_lock = threading.Lock()

    def _fake_post(base_url, path, payload, headers=None):
        with calls_lock:
            post_calls.append(payload)
        call_started.set()
        # Held open deliberately -- simulates the slow bureau/model call --
        # so the racing thread's attempt below happens while this one is
        # still genuinely 'in_progress', not after it already finished.
        release_call.wait(timeout=5)
        return {
            "outcome": "approve", "score": 700, "reason": None,
            "attempt_id": payload["attempt_id"],
            "bureau_score": 680, "model_version": "v1-stub", "top_features": None,
            "reason_codes": [],
        }

    monkeypatch.setattr(clients, "post", _fake_post)

    winner = {}
    loser = {}

    def _winner_thread():
        attempt_id = decision_state.start_decision_attempt(app_id, "borrower")
        winner["attempt_id"] = attempt_id
        resp = clients.post(clients.DECISION_URL, "/decisions", {
            "application_id": app_id, "attempt_id": attempt_id,
        })
        winner["resp"] = resp
        # Complete TXN B for real, using the same functions/statements
        # run_decision itself uses, so "no duplicate decision or
        # decision_event" is proven against the real persisted rows, not
        # just the attempt-creation step.
        with db.transaction() as cur:
            funded, manual = decision_state.recheck_finality_locked(cur, app_id)
            assert not funded and not manual
            cur.execute("INSERT INTO decisions (app_id, outcome) VALUES (%s, %s)", (app_id, resp["outcome"]))
            cur.execute(
                "INSERT INTO decision_events (app_id, model_version, decision, attempt_id) "
                "VALUES (%s, %s, %s, %s)",
                (app_id, resp["model_version"], resp["outcome"], attempt_id),
            )
            cur.execute(
                "UPDATE decision_attempts SET state = 'completed', completed_at = now() "
                "WHERE id = %s AND state = 'in_progress'",
                (attempt_id,),
            )

    def _loser_thread():
        # Only attempt once the winner's external call has genuinely
        # started -- proves this is a real race against an in-flight
        # attempt, not a guess about timing.
        assert call_started.wait(timeout=5)
        try:
            decision_state.start_decision_attempt(app_id, "borrower")
            loser["outcome"] = "unexpectedly_succeeded"
        except HTTPException as e:
            loser["status_code"] = e.status_code
            loser["detail"] = e.detail

    t_winner = threading.Thread(target=_winner_thread)
    t_loser = threading.Thread(target=_loser_thread)
    t_winner.start()
    t_loser.start()
    t_loser.join(timeout=10)
    release_call.set()
    t_winner.join(timeout=10)

    # The loser was rejected with the documented 409, before ever touching
    # the external call -- no bureau/model work.
    assert loser.get("status_code") == 409
    assert "already in progress" in loser["detail"]

    # Exactly one external call was ever made, by the winner, under its own
    # attempt_id.
    assert len(post_calls) == 1
    assert post_calls[0]["attempt_id"] == winner["attempt_id"]

    with real_db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT count(*) AS n FROM decision_attempts WHERE app_id = %s", (app_id,))
        assert cur.fetchone()["n"] == 1, "exactly one attempt row -- the loser's INSERT must never have run"
        cur.execute("SELECT count(*) AS n FROM decisions WHERE app_id = %s", (app_id,))
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT count(*) AS n FROM decision_events WHERE app_id = %s", (app_id,))
        assert cur.fetchone()["n"] == 1, "no duplicate decision_events row"


# --- Test 2: a failure partway through TXN B rolls everything back ---------

def test_txn_b_failure_after_a_write_rolls_back_decision_event_and_attempt_together(
    real_db, monkeypatch
):
    """Injected failure point: the mocked decision-service response omits
    model_version (None) -- decision_events.model_version is a real
    NOT NULL column (same DDL migration 0023 and db/init/006 both ship),
    so the INSERT INTO decision_events statement inside TXN B raises a
    genuine psycopg2.errors.NotNullViolation. This happens AFTER the
    INSERT INTO decisions and UPDATE applications SET status statements in
    the same real run_decision code path have already executed
    (uncommitted) -- a real constraint violation, not a test-only hook,
    and no production code is changed to make this possible.

    PR #6 review (lease-invariant follow-up): run_decision now catches this
    class of failure itself -- TXN B's own db.transaction() context manager
    has already rolled everything back by the time the exception reaches
    the except clause; run_decision then marks the attempt 'failed'
    (failure_code='persistence_error') in a SEPARATE short transaction and
    returns a clean 500, so a retry can proceed IMMEDIATELY -- it no longer
    has to wait for the lease to expire. Lease expiry remains only the
    fallback for a failure this code can never reach (the process itself
    dying mid-transaction, proven separately by
    test_stale_lease_is_atomically_expired_and_replaced in
    db/tests/test_decision_attempt_lifecycle.py)."""
    app_id = 502
    _seed_application(real_db, app_id, status="submitted")

    offer_calls = []
    monkeypatch.setattr(disclosure_graph, "auto_generate_offer", lambda app_id: offer_calls.append(app_id))

    def _fake_post(base_url, path, payload, headers=None):
        return {
            "outcome": "approve", "score": 700, "reason": None,
            "attempt_id": payload["attempt_id"],
            "bureau_score": 680,
            "model_version": None,  # <-- the injected constraint violation
            "top_features": None, "reason_codes": [],
        }

    monkeypatch.setattr(clients, "post", _fake_post)

    resp = client.post("/applications/502/decision", json={"access_token": "real-access-token"})

    # run_decision's own except clause now converts this into a clean,
    # documented 500 -- never a 200, never a partial success, and no
    # longer an unhandled exception either.
    assert resp.status_code == 500
    assert resp.json()["detail"] == "could not persist the decision -- please retry"

    with real_db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")

        # No authoritative decision survived.
        cur.execute("SELECT count(*) AS n FROM decisions WHERE app_id = %s", (app_id,))
        assert cur.fetchone()["n"] == 0

        # No decision_event survived (the very statement that failed).
        cur.execute("SELECT count(*) AS n FROM decision_events WHERE app_id = %s", (app_id,))
        assert cur.fetchone()["n"] == 0

        # The attempt was marked 'failed' -- immediately, in the separate
        # transaction run_decision's except clause opens -- not left
        # 'in_progress' to be discovered only later via lease expiry.
        cur.execute("SELECT id, state, failure_code, failure_detail FROM decision_attempts WHERE app_id = %s", (app_id,))
        attempt_row = cur.fetchone()
        attempt_id = attempt_row["id"]
        assert attempt_row["state"] == "failed"
        assert attempt_row["failure_code"] == "persistence_error"
        # The failure detail is sanitized -- one of the fixed templates,
        # never a raw exception message/constraint name/column value.
        assert attempt_row["failure_detail"] == decision_state._FAILURE_DETAIL["persistence_error"]
        assert "NotNullViolation" not in attempt_row["failure_detail"]
        assert "model_version" not in attempt_row["failure_detail"]

        # No approval accept-token hash/expiry survived -- issue_accept_token
        # ran (if at all) inside the same rolled-back transaction.
        cur.execute(
            "SELECT status, accept_token_hash, accept_token_expires_at FROM applications WHERE id = %s",
            (app_id,),
        )
        app_row = cur.fetchone()
        assert app_row["status"] == "submitted", "applications.status must be unchanged -- the UPDATE rolled back too"
        assert app_row["accept_token_hash"] is None
        assert app_row["accept_token_expires_at"] is None

    # Offer generation (a real side effect gated on a committed 'approve')
    # must never have been invoked -- the request never reached that code
    # at all, since the exception propagated out of the `with
    # db.transaction()` block before it.
    assert offer_calls == []

    # Retry is available immediately -- the attempt is already terminal
    # ('failed'), so a fresh start_decision_attempt call succeeds right
    # away, with no sleep/lease-wait required at all.
    new_attempt_id = decision_state.start_decision_attempt(app_id, "borrower")
    assert new_attempt_id != attempt_id

    with real_db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT state, failure_code FROM decision_attempts WHERE id = %s", (attempt_id,))
        still_failed = cur.fetchone()
        assert still_failed["state"] == "failed"
        assert still_failed["failure_code"] == "persistence_error"
        cur.execute("SELECT state FROM decision_attempts WHERE id = %s", (new_attempt_id,))
        assert cur.fetchone()["state"] == "in_progress"
