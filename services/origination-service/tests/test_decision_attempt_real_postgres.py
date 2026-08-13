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
  3. TXN B and a concurrent TXN A take their two row locks in the SAME
     order (applications -> decision_attempts) and therefore cannot
     deadlock -- the A2 regression. This one is written specifically to
     FAIL against the previous lock order, not merely to pass; see its
     docstring for why the interleaving is deterministic in both
     directions.

Both tests point the REAL app.db module at a throwaway Postgres schema
(dropped/recreated per test) instead of the mocked db.transaction()/
db.query() every other test in this directory uses -- only the HTTP
boundary to decision-service (app.clients.post) is mocked, exactly what
"mocking only the external HTTP boundary is acceptable" calls for.
"""
import os
import threading
from pathlib import Path

import httpx
import psycopg2
import psycopg2.extras
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app import clients, config, db, decision_state, disclosure_graph
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
            status TEXT DEFAULT 'submitted',
            access_token_hash TEXT,
            access_token_expires_at TIMESTAMPTZ,
            access_token_consumed_at TIMESTAMPTZ,
            -- db/migrations/0039. Another hand-written copy of
            -- db/init/001_schema.sql; ACCESS_TOKEN_FIELDS selects these,
            -- so omitting them fails the read, not the feature.
            prev_access_token_hash TEXT,
            prev_access_token_expires_at TIMESTAMPTZ,
            accept_token_hash TEXT,
            accept_token_expires_at TIMESTAMPTZ,
            accept_token_consumed_at TIMESTAMPTZ,
            idempotency_key TEXT,
                resume_token_hash TEXT,
                resume_token_expires_at TIMESTAMPTZ,
                resume_token_consumed_at TIMESTAMPTZ
        );
            CREATE UNIQUE INDEX applications_idempotency_key_uniq
                ON applications (idempotency_key) WHERE idempotency_key IS NOT NULL;
        CREATE TABLE kyc_checks (
            id SERIAL PRIMARY KEY,
            applicant_id INTEGER REFERENCES applicants(id),
                application_id INTEGER REFERENCES applications(id),
            name_verified BOOLEAN, dob_verified BOOLEAN,
            address_verified BOOLEAN, ssn_verified BOOLEAN,
                cip_passed BOOLEAN,
            created_at TIMESTAMPTZ DEFAULT now()
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
        -- Canonical offer row (db/init/001_schema.sql). Nullable amounts on
        -- purpose: the Gap F tests seed deliberately incomplete rows to prove
        -- the read/accept paths refuse them. The production schema carries a
        -- CHECK (offers_canonical_terms_present) that would block that seeding,
        -- which is precisely why these tests assert the APPLICATION-level
        -- refusal rather than relying on the constraint alone.
        CREATE TABLE offers (
            id SERIAL PRIMARY KEY,
            app_id INTEGER REFERENCES applications(id) UNIQUE,
            decision_id INTEGER REFERENCES decisions(app_id) UNIQUE,
            fee_pct_used NUMERIC(5,4),
            note_rate_pct NUMERIC(7,3),
            apr NUMERIC(7,3),
            finance_charge NUMERIC(14,2),
            monthly_payment NUMERIC(14,2),
            amount_financed NUMERIC(14,2),
            total_of_payments NUMERIC(14,2),
            -- Model B schedule facts (db/migrations/0030). Boarding requires
            -- these, so a fixture omitting them cannot board.
            regular_payment_count INTEGER,
            final_payment NUMERIC(14,2),
            term_months INTEGER,
            schedule_version TEXT,
            -- The principal the stored schedule was solved for; boarding opens
            -- the loan at this value (db/migrations/0030).
            principal NUMERIC(14,2),
            created_at TIMESTAMPTZ DEFAULT now(),
            accepted_at TIMESTAMPTZ
        );
        -- Boarding sink (db/init/001_schema.sql:117-138). Present so the A3
        -- route-removal test can assert on REAL row counts rather than on a
        -- spy for one particular implementation of boarding.
        CREATE TABLE loans (
            id SERIAL PRIMARY KEY,
            app_id INTEGER UNIQUE,
            applicant_name TEXT,
            principal NUMERIC(14,2) NOT NULL,
            apr NUMERIC(7,3) NOT NULL,
            term_months INTEGER NOT NULL,
            -- The Model B contract as boarded (db/migrations/0030), WITH its
            -- constraints. Copying only the columns would let these tests pass
            -- on a boarding write that real Postgres rejects -- and the whole
            -- point of a real-Postgres fixture is that it does not diverge
            -- from the deployed schema in ways the tests cannot see.
            regular_payment NUMERIC(14,2),
            regular_payment_count INTEGER,
            final_payment NUMERIC(14,2),
            schedule_version TEXT,
            status TEXT DEFAULT 'current',
            opened_at TIMESTAMPTZ DEFAULT now(),
            CONSTRAINT loans_schedule_all_or_nothing CHECK (
                (regular_payment IS NULL AND regular_payment_count IS NULL
                 AND final_payment IS NULL AND schedule_version IS NULL)
                OR
                (regular_payment IS NOT NULL AND regular_payment_count IS NOT NULL
                 AND final_payment IS NOT NULL AND schedule_version IS NOT NULL)
            ),
            CONSTRAINT loans_schedule_term_agrees CHECK (
                regular_payment_count IS NULL OR regular_payment_count + 1 = term_months
            ),
            CONSTRAINT loans_schedule_amounts_positive CHECK (
                (regular_payment IS NULL OR regular_payment > 0)
                AND (final_payment IS NULL OR final_payment > 0)
                AND (regular_payment_count IS NULL OR regular_payment_count >= 0)
            ),
            CONSTRAINT loans_schedule_version_supported CHECK (
                schedule_version IS NULL OR schedule_version IN ('B1')
            )
        );
        CREATE TABLE balances (
            loan_id INTEGER PRIMARY KEY REFERENCES loans(id),
            balance NUMERIC(14,2) NOT NULL,
            past_due NUMERIC(14,2) DEFAULT 0,
            updated_at TIMESTAMPTZ DEFAULT now()
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
        cur.execute((MIGRATIONS_DIR / "0024_decision_attempt_bureau_key.sql").read_text())
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
    """Seeds a live submission token the same way intake does: only the sha256
    hash is stored, with a Postgres-clock expiry (Gap B)."""
    with conn.cursor() as c:
        c.execute(f"SET search_path TO {SCHEMA}")
        c.execute(
            "INSERT INTO applicants (id, name, ssn) VALUES (%s, %s, %s)",
            (app_id, "Jane Borrower", "123456781"),
        )
        c.execute(
            "INSERT INTO applications (id, applicant_id, amount, term_months, income, "
            "access_token_hash, access_token_expires_at, status) "
            "VALUES (%s, %s, %s, %s, %s, %s, now() + interval '1 hour', %s)",
            (app_id, app_id, amount, term_months, income,
             decision_state.hash_access_token(access_token), status),
        )
        # PR #18: run_decision refuses an application with no persisted KYC
        # result. These fixtures exercise the decision-attempt lease, not
        # identity verification, so they get a recorded result.
        c.execute(
            # cip_passed, not just the factors: the decision gate reads the
            # VERDICT now (db/migrations/0033), because a row that merely
            # existed used to satisfy it even when CIP had failed.
            "INSERT INTO kyc_checks (applicant_id, application_id, name_verified, "
            "cip_passed) VALUES (%s, %s, true, true)",
            (app_id, app_id),
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
        attempt_id, _key = decision_state.start_decision_attempt(app_id, "borrower")
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
    new_attempt_id, _ = decision_state.start_decision_attempt(app_id, "borrower")
    assert new_attempt_id != attempt_id

    with real_db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT state, failure_code FROM decision_attempts WHERE id = %s", (attempt_id,))
        still_failed = cur.fetchone()
        assert still_failed["state"] == "failed"
        assert still_failed["failure_code"] == "persistence_error"
        cur.execute("SELECT state FROM decision_attempts WHERE id = %s", (new_attempt_id,))
        assert cur.fetchone()["state"] == "in_progress"


# --- Test 3 (A2): TXN B vs a concurrent TXN A cannot deadlock ----------------

def test_txn_b_and_concurrent_txn_a_do_not_deadlock(real_db, monkeypatch):
    """A2 regression. Deliberately builds the interleaving that USED to
    deadlock, and asserts Postgres never raises DeadlockDetected.

    Global lock order under test: applications -> decision_attempts.

    How the interleaving is forced deterministically, in BOTH directions:

      * Thread 1 runs the REAL POST /{app_id}/decision endpoint. Both TXN B
        helpers are wrapped so that whichever one runs FIRST (i.e. whichever
        takes TXN B's first row lock) signals `t1_holds_first` and then waits
        on `t2_holds_first` before TXN B is allowed to take its second lock.
        The wrapper is order-agnostic on purpose -- it instruments "the first
        lock", not a specific table -- so the same test body exercises the
        old order and the new one.
      * Thread 2 replays TXN A's real statement sequence from
        decision_state.start_decision_attempt (applications FOR UPDATE at
        decision_state.py:80, then the in_progress decision_attempts
        FOR UPDATE at :166-171) on its own connection, signalling
        `t2_holds_first` once it holds ITS first lock.

    Old order (decision_attempts -> applications):
      T1 locks the attempt row and signals. T2 is free to take `applications`
      (T1 doesn't hold it), so T2 signals immediately. T1 then reaches for
      `applications` (held by T2) and T2 reaches for the attempt row (held by
      T1) -> genuine ABBA cycle -> Postgres aborts one side with
      DeadlockDetected and this test FAILS.

    New order (applications -> decision_attempts):
      T1 locks `applications` first. T2's very first statement wants the same
      row, so T2 blocks and never signals; T1's wait simply times out after
      _SIGNAL_TIMEOUT and it proceeds to take the attempt row (free) and
      commit. T2 then unblocks and runs to completion. No cycle is ever
      possible, because neither thread can hold `decision_attempts` while
      waiting on `applications`.
    """
    app_id = 503
    _seed_application(real_db, app_id)

    _SIGNAL_TIMEOUT = 3.0
    t1_holds_first = threading.Event()
    t2_holds_first = threading.Event()

    post_calls = []
    monkeypatch.setattr(disclosure_graph, "auto_generate_offer", lambda app_id: None)

    def _fake_post(base_url, path, payload, headers=None):
        post_calls.append(payload)
        return {
            "outcome": "approve", "score": 700, "reason": None,
            "attempt_id": payload["attempt_id"],
            "bureau_score": 680, "model_version": "v1-stub", "top_features": None,
            "reason_codes": [],
        }

    monkeypatch.setattr(clients, "post", _fake_post)

    # Instrument TXN B's two lock-taking helpers. `first_done` makes the
    # signal+wait fire exactly once -- after TXN B's FIRST lock, whichever
    # helper that happens to be under the ordering being tested.
    #
    # `phase` is load-bearing: start_decision_attempt (TXN A) calls
    # recheck_finality_locked as a BARE NAME from inside decision_state, and
    # monkeypatching the module attribute rebinds that global too -- so
    # without this guard the instrumentation fires on TXN A's applications
    # lock instead of TXN B's first lock, the intended interleaving never
    # happens, and the test passes against BOTH orderings (verified: it did).
    real_start = decision_state.start_decision_attempt
    real_recheck = decision_state.recheck_finality_locked
    real_verify = decision_state.verify_attempt_still_active_locked
    phase = {"txn_b": False}
    first_done = []

    def _wrapped_start(aid, requested_by):
        result = real_start(aid, requested_by)
        phase["txn_b"] = True   # TXN A has committed; the next locks are TXN B's
        return result

    def _after_first_lock():
        if not phase["txn_b"] or first_done:
            return
        first_done.append(True)
        t1_holds_first.set()
        t2_holds_first.wait(timeout=_SIGNAL_TIMEOUT)

    def _wrapped_recheck(cur, aid):
        result = real_recheck(cur, aid)
        _after_first_lock()
        return result

    def _wrapped_verify(cur, aid):
        result = real_verify(cur, aid)
        _after_first_lock()
        return result

    monkeypatch.setattr(decision_state, "start_decision_attempt", _wrapped_start)
    monkeypatch.setattr(decision_state, "recheck_finality_locked", _wrapped_recheck)
    monkeypatch.setattr(decision_state, "verify_attempt_still_active_locked", _wrapped_verify)

    t1_result, t2_result = {}, {}

    def _thread1_real_endpoint():
        try:
            resp = client.post(
                f"/applications/{app_id}/decision", json={"access_token": "real-access-token"}
            )
            t1_result["status_code"] = resp.status_code
        except Exception as e:  # noqa -- recorded, asserted on below
            t1_result["exception"] = e

    def _thread2_txn_a_replica():
        """Replays start_decision_attempt's real statement order."""
        if not t1_holds_first.wait(timeout=_SIGNAL_TIMEOUT):
            t2_result["outcome"] = "t1_never_signalled"
            return
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(f"SET search_path TO {SCHEMA}")
                # TXN A lock #1 -- applications (decision_state.py:80)
                cur.execute("SELECT status FROM applications WHERE id = %s FOR UPDATE", (app_id,))
                cur.fetchall()
                t2_holds_first.set()
                # TXN A lock #2 -- decision_attempts (decision_state.py:166-171)
                cur.execute(
                    "SELECT id, (lease_expires_at > now()) AS live FROM decision_attempts "
                    "WHERE app_id = %s AND state = 'in_progress' FOR UPDATE",
                    (app_id,),
                )
                cur.fetchall()
            conn.commit()
            t2_result["outcome"] = "completed"
        except psycopg2.errors.DeadlockDetected as e:
            conn.rollback()
            t2_result["deadlock"] = str(e)
        finally:
            t2_holds_first.set()  # never strand thread 1
            conn.close()

    t1 = threading.Thread(target=_thread1_real_endpoint)
    t2 = threading.Thread(target=_thread2_txn_a_replica)
    t1.start(); t2.start()
    t1.join(timeout=30); t2.join(timeout=30)
    assert not t1.is_alive() and not t2.is_alive(), "threads did not finish -- lock cycle never broke"

    # --- The A2 assertion: neither side deadlocked. ---
    assert "deadlock" not in t2_result, f"TXN A deadlocked against TXN B: {t2_result.get('deadlock')}"
    t1_exc = t1_result.get("exception")
    assert not isinstance(t1_exc, psycopg2.errors.DeadlockDetected), f"TXN B deadlocked: {t1_exc}"
    assert t1_exc is None, f"TXN B raised unexpectedly: {t1_exc!r}"
    # A deadlock inside TXN B is swallowed by run_decision's own
    # `except Exception` and surfaces as 500 persistence_error -- so the
    # status code has to be checked too, not just the raised exception.
    assert t1_result.get("status_code") == 200, t1_result

    # Deterministic outcome for the other side.
    assert t2_result.get("outcome") == "completed", t2_result

    # Exactly one bureau/model call, and no duplicated rows.
    assert len(post_calls) == 1

    with real_db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT count(*) AS n FROM decision_attempts WHERE app_id = %s", (app_id,))
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT state, failure_code FROM decision_attempts WHERE app_id = %s", (app_id,))
        row = cur.fetchone()
        assert row["state"] == "completed" and row["failure_code"] is None
        cur.execute("SELECT count(*) AS n FROM decisions WHERE app_id = %s", (app_id,))
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT count(*) AS n FROM decision_events WHERE app_id = %s", (app_id,))
        assert cur.fetchone()["n"] == 1


# --- Test 4: TXN-B failure logging is type-only (no DB text, no values) ------

_CANARY = "CANARY-7f3a9d21-SHOULD-NOT-BE-LOGGED"


def test_txn_b_persistence_failure_logs_only_the_error_type(real_db, monkeypatch, caplog):
    """A database error's message carries the failing SQL, the constraint name
    AND the offending parameter values, so logging the exception object puts
    decision inputs into the service log.

    Injection: decision-service returns `bureau_score` = a recognizable canary
    string. decision_events.bureau_score is INTEGER, so Postgres raises
    `invalid input syntax for type integer: "CANARY-..."` -- a real DB error
    whose text embeds a value this service did not author. That is exactly the
    leak class under test, and it reaches TXN B's own except clause."""
    import logging

    app_id = 504
    _seed_application(real_db, app_id)
    monkeypatch.setattr(disclosure_graph, "auto_generate_offer", lambda app_id: None)

    def _fake_post(base_url, path, payload, headers=None):
        return {
            "outcome": "approve", "score": 700, "reason": None,
            "attempt_id": payload["attempt_id"],
            "bureau_score": _CANARY,          # <-- the injected canary
            "model_version": "v1-stub", "top_features": None, "reason_codes": [],
        }

    monkeypatch.setattr(clients, "post", _fake_post)

    caplog.set_level(logging.ERROR)
    resp = client.post(f"/applications/{app_id}/decision", json={"access_token": "real-access-token"})

    # Caller sees only the generic message -- never the DB text.
    assert resp.status_code == 500
    assert resp.json()["detail"] == "could not persist the decision -- please retry"
    assert _CANARY not in resp.text

    logged = caplog.text
    # 1. The canary never reaches the log.
    assert _CANARY not in logged, "raw parameter value leaked into the log"
    # 2. Nor does any SQL / constraint / column detail from the DB message.
    for fragment in ("invalid input syntax", "INSERT INTO", "bureau_score", "LINE ", "DETAIL:"):
        assert fragment not in logged, f"database detail {fragment!r} leaked into the log"
    # 3. The error TYPE is still recorded, so the failure stays diagnosable.
    assert "error_type=" in logged
    assert "TXN B failed to persist" in logged
    assert f"app_id={app_id}" in logged

    # 4. Rollback and cleanup are unaffected by the logging change.
    with real_db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT count(*) AS n FROM decisions WHERE app_id = %s", (app_id,))
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT count(*) AS n FROM decision_events WHERE app_id = %s", (app_id,))
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT state, failure_code FROM decision_attempts WHERE app_id = %s", (app_id,))
        row = cur.fetchone()
        assert row["state"] == "failed"
        assert row["failure_code"] == "persistence_error"
        cur.execute("SELECT status, accept_token_hash FROM applications WHERE id = %s", (app_id,))
        app_row = cur.fetchone()
        assert app_row["status"] == "submitted"
        assert app_row["accept_token_hash"] is None


# --- Test 5 (A3): the removed /board route writes no loan or balance --------

def test_removed_board_route_creates_no_loan_or_balance_rows(real_db):
    """A3 proven against real table state rather than a spy on one particular
    boarding helper. The previous version patched intake.board_to_servicing,
    which would catch only a verbatim restoration of the old route -- a /board
    re-added calling board_to_servicing_tx, or raw SQL, would have passed it.
    Counting rows fails for ANY implementation that boards."""
    def _counts():
        with real_db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SET search_path TO {SCHEMA}")
            cur.execute("SELECT count(*) AS n FROM loans")
            loans = cur.fetchone()["n"]
            cur.execute("SELECT count(*) AS n FROM balances")
            return loans, cur.fetchone()["n"]

    before = _counts()

    resp = client.post("/board", json={
        "app_id": 9001, "applicant_name": "Attacker", "principal": 50000,
        "annual_rate_pct": 0.01, "term_months": 48,
    })

    assert resp.status_code in (404, 405), "POST /board must not be routable"
    assert "loan_id" not in resp.text
    assert _counts() == before, "a loan or balance row was written by the removed route"


# --- Test 6 (Gap A): ambiguous timeout + retry => ONE bureau pull -----------

def test_ambiguous_timeout_retry_reuses_the_key_and_commits_one_event(real_db, monkeypatch):
    """The exact Gap A scenario, end to end against real Postgres:

      1. the bureau operation COMPLETES,
      2. origination loses the response to a timeout (the ambiguous case --
         indistinguishable from "the bureau never ran"),
      3. the borrower retries,
      4. the bureau operation is performed exactly ONCE,
      5. the original result is recovered via the stable request key,
      6. exactly one permanent decision_events row is committed.

    The fake below stands in for decision-service AND the bureau behind it: it
    records a pull per distinct bureau_request_key, exactly as
    decision-service's StubBureauClient does (see
    decision-service/tests/test_bureau_idempotency.py for that half). The first
    call records the pull and THEN raises a timeout -- modelling "the bureau ran
    and the response was lost", not "the bureau never ran"."""
    app_id = 505
    _seed_application(real_db, app_id)
    monkeypatch.setattr(disclosure_graph, "auto_generate_offer", lambda app_id: None)

    pulls_by_key: dict[str, dict] = {}
    keys_seen: list[str] = []
    calls = {"n": 0}

    def _fake_post(base_url, path, payload, headers=None):
        key = payload["bureau_request_key"]
        keys_seen.append(key)
        # Bureau-side idempotency: one real pull per distinct key.
        if key not in pulls_by_key:
            pulls_by_key[key] = {
                "bureau_score": 680,
                "bureau_reference_id": f"stub-{key}",
            }
        pulled = pulls_by_key[key]

        calls["n"] += 1
        if calls["n"] == 1:
            # The bureau finished (recorded above) but we never see the answer.
            raise httpx.ReadTimeout("simulated ambiguous timeout")

        return {
            "outcome": "approve", "score": 700, "reason": None,
            "attempt_id": payload["attempt_id"],
            "bureau_score": pulled["bureau_score"],
            "bureau_reference_id": pulled["bureau_reference_id"],
            "model_version": "v1-stub", "top_features": None, "reason_codes": [],
        }

    monkeypatch.setattr(clients, "post", _fake_post)

    # 1-2. First request: bureau ran, response lost.
    first = client.post(f"/applications/{app_id}/decision", json={"access_token": "real-access-token"})
    assert first.status_code == 502
    assert "timed out" in first.json()["detail"]

    with real_db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT state, failure_code, bureau_request_key FROM decision_attempts WHERE app_id = %s", (app_id,))
        timed_out = cur.fetchone()
        assert timed_out["state"] == "failed"
        assert timed_out["failure_code"] == "timeout"
        original_key = timed_out["bureau_request_key"]
        assert original_key, "the attempt must record the key it presented to the bureau"

    # 3. Retry.
    second = client.post(f"/applications/{app_id}/decision", json={"access_token": "real-access-token"})
    assert second.status_code == 200
    assert second.json()["decision"] == "approve"

    # 4-5. Same key both times => exactly one bureau operation, original result.
    assert len(keys_seen) == 2, "decision-service should have been called twice"
    assert keys_seen[0] == keys_seen[1] == original_key, (
        "the retry must present the SAME bureau_request_key so the provider "
        "returns the original operation instead of pulling again"
    )
    assert len(pulls_by_key) == 1, "the bureau was pulled more than once"

    # 6. Exactly one permanent decision event, carrying the recovered reference.
    with real_db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT count(*) AS n FROM decision_events WHERE app_id = %s", (app_id,))
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT count(*) AS n FROM decisions WHERE app_id = %s", (app_id,))
        assert cur.fetchone()["n"] == 1
        cur.execute(
            "SELECT state, bureau_reference_id FROM decision_attempts "
            "WHERE app_id = %s ORDER BY id DESC LIMIT 1", (app_id,)
        )
        completed = cur.fetchone()
        assert completed["state"] == "completed"
        assert completed["bureau_reference_id"] == f"stub-{original_key}"


def test_a_genuinely_new_decision_request_gets_a_fresh_bureau_key(real_db, monkeypatch):
    """Guard against the fix degenerating into a credit-data cache. A staff
    rerun after a COMPLETED decision is a genuinely new request and must mint a
    new key, so it performs a real new pull rather than replaying a stale
    score."""
    app_id = 506
    _seed_application(real_db, app_id)
    monkeypatch.setattr(disclosure_graph, "auto_generate_offer", lambda app_id: None)

    keys_seen: list[str] = []

    def _fake_post(base_url, path, payload, headers=None):
        keys_seen.append(payload["bureau_request_key"])
        return {
            "outcome": "approve", "score": 700, "reason": None,
            "attempt_id": payload["attempt_id"],
            "bureau_score": 680, "bureau_reference_id": f"stub-{payload['bureau_request_key']}",
            "model_version": "v1-stub", "top_features": None, "reason_codes": [],
        }

    monkeypatch.setattr(clients, "post", _fake_post)

    first = client.post(f"/applications/{app_id}/decision", json={"access_token": "real-access-token"})
    assert first.status_code == 200

    # Staff rerun -- a new logical decision request, not a retry.
    second = client.post(
        f"/applications/{app_id}/decision",
        headers={"X-User-Role": "underwriter", "X-Internal-Token": config.INTERNAL_SERVICE_TOKEN},
    )
    assert second.status_code == 200

    assert len(keys_seen) == 2
    assert keys_seen[0] != keys_seen[1], (
        "a completed predecessor means a NEW decision request -- reusing the key "
        "would serve stale credit data"
    )


# --- Test 8 (Gap F): incomplete offer terms can never board a loan ----------

_CANONICAL_TERMS = ("apr", "finance_charge", "monthly_payment", "amount_financed",
                    "total_of_payments")


@pytest.mark.parametrize("missing_field", _CANONICAL_TERMS)
def test_incomplete_offer_terms_never_board_a_loan(real_db, monkeypatch, missing_field):
    """Gap F, the part that actually moves money: accept_offer used to check
    `apr IS NULL` alone, so an offers row missing finance_charge,
    monthly_payment, amount_financed or total_of_payments still boarded a real
    loan -- funding a borrower on terms they were never shown a complete
    disclosure for. All five are re-checked under the row lock now.

    One case per canonical amount, asserted against real loans/balances counts
    rather than a mocked cursor."""
    app_id = 520 + _CANONICAL_TERMS.index(missing_field)
    _seed_application(real_db, app_id, status="approved")

    complete = {
        "apr": 5.946, "finance_charge": 768.11, "monthly_payment": 407.0,
        "amount_financed": 8730.0, "total_of_payments": 9768.11,
    }
    terms = dict(complete, **{missing_field: None})
    raw_accept = "borrower-accept-token-for-gap-f"

    with real_db.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("INSERT INTO decisions (app_id, outcome) VALUES (%s, 'approve')", (app_id,))
        cur.execute(
            # Boarding requires the stored Model B schedule; an offer without it
            # is a legacy row that cannot board.
            "INSERT INTO offers (app_id, decision_id, fee_pct_used, note_rate_pct, apr, "
            "finance_charge, monthly_payment, amount_financed, total_of_payments, "
            "regular_payment_count, final_payment, term_months, schedule_version, principal) "
            "VALUES (%s, %s, 0.03, 7.990, %s, %s, %s, %s, %s, 23, 407.12, 24, 'B1', 9000.00)",
            (app_id, app_id, terms["apr"], terms["finance_charge"], terms["monthly_payment"],
             terms["amount_financed"], terms["total_of_payments"]),
        )
        cur.execute(
            "UPDATE applications SET accept_token_hash = %s, "
            "accept_token_expires_at = now() + interval '1 hour' WHERE id = %s",
            (decision_state.hash_accept_token(raw_accept), app_id),
        )
    real_db.commit()

    def _counts():
        with real_db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SET search_path TO {SCHEMA}")
            cur.execute("SELECT count(*) AS n FROM loans")
            loans = cur.fetchone()["n"]
            cur.execute("SELECT count(*) AS n FROM balances")
            return loans, cur.fetchone()["n"]

    before = _counts()
    resp = client.post(
        f"/applications/{app_id}/accept",
        headers={"X-Offer-Accept-Token": raw_accept},
    )

    assert resp.status_code == 409, f"a NULL {missing_field} must not board a loan"
    assert missing_field in resp.json()["detail"]
    assert _counts() == before, f"a loan/balance was boarded despite a NULL {missing_field}"


def test_a_complete_offer_still_boards_exactly_one_loan_and_balance(real_db, monkeypatch):
    """Regression guard for Gap F: the integrity check must not break the
    supported accept path."""
    app_id = 530
    _seed_application(real_db, app_id, status="approved")
    raw_accept = "borrower-accept-token-complete"

    with real_db.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("INSERT INTO decisions (app_id, outcome) VALUES (%s, 'approve')", (app_id,))
        cur.execute(
            "INSERT INTO offers (app_id, decision_id, fee_pct_used, note_rate_pct, apr, "
            "finance_charge, monthly_payment, amount_financed, total_of_payments, "
            "regular_payment_count, final_payment, term_months, schedule_version, principal) "
            "VALUES (%s, %s, 0.03, 7.990, 5.946, 768.11, 407.0, 8730.0, 9768.11, "
            "23, 407.12, 24, 'B1', 9000.00)",
            (app_id, app_id),
        )
        cur.execute(
            "UPDATE applications SET accept_token_hash = %s, "
            "accept_token_expires_at = now() + interval '1 hour' WHERE id = %s",
            (decision_state.hash_accept_token(raw_accept), app_id),
        )
    real_db.commit()

    resp = client.post(
        f"/applications/{app_id}/accept",
        headers={"X-Offer-Accept-Token": raw_accept},
    )

    assert resp.status_code == 200
    with real_db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT count(*) AS n FROM loans WHERE app_id = %s", (app_id,))
        assert cur.fetchone()["n"] == 1
        cur.execute("SELECT count(*) AS n FROM balances")
        assert cur.fetchone()["n"] == 1
