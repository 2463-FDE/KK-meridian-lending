"""Real, multi-connection proof for the decision-attempt/reservation
lifecycle (PR #6 review, Finding 2 -- the corrected design, not the
narrowing pre-call/post-call check pair that was rejected).

Everything else testing this logic (services/origination-service/tests/
test_decision_and_accept_authz.py) mocks db.transaction() with a single
in-process fake cursor -- that proves the application code issues the
right SQL in the right order, but it cannot prove the SQL itself is safe
under real concurrent access or that the database's own constraints hold.
This file uses independent, real psycopg2 connections (threading.Barrier
where genuine concurrency matters, or a plain sequential replay where the
property under test is about transaction BOUNDARIES rather than a race)
against the actual DDL in db/migrations/0023_decision_attempts.sql, applied
to a throwaway schema.

Architecture under test: origination-service creates a decision_attempts
row (TXN A, applications row locked, released before any network call) --
decision-service is compute-only and never writes decision_events itself --
origination-service takes the lock again afterward (TXN B) and only THEN
writes decisions + decision_events + marks the attempt completed,
atomically, and only on the branch where it still wins the finality race.
"""
import os
import threading
import time
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"

SCHEMA = "attempt_lifecycle_test"


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
            CREATE TABLE applications (id SERIAL PRIMARY KEY, status TEXT DEFAULT 'submitted');
            CREATE TABLE decisions (app_id INTEGER PRIMARY KEY REFERENCES applications(id), outcome TEXT NOT NULL);
            CREATE TABLE manual_reviews (
                id SERIAL PRIMARY KEY,
                app_id INTEGER NOT NULL REFERENCES applications(id) UNIQUE,
                reviewer_role TEXT NOT NULL,
                outcome TEXT NOT NULL,
                reason TEXT NOT NULL,
                reviewed_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
            CREATE TABLE decision_events (
                id SERIAL PRIMARY KEY,
                app_id INTEGER NOT NULL REFERENCES applications(id),
                model_version TEXT NOT NULL,
                decision TEXT NOT NULL
            );
        """)
    connection.commit()
    _run_sql_file(connection, MIGRATIONS_DIR / "0023_decision_attempts.sql")
    yield connection
    cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    connection.commit()
    connection.close()


def _run_sql_file(conn, path):
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(path.read_text())
    conn.commit()


def _new_conn():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn


def _seed_app(conn, app_id, status="submitted"):
    with conn.cursor() as c:
        c.execute(f"SET search_path TO {SCHEMA}")
        c.execute("INSERT INTO applications (id, status) VALUES (%s, %s)", (app_id, status))
    conn.commit()


# --- TXN A: start_decision_attempt's real statement sequence ----------------

def _start_attempt(app_id, lease_seconds, result, key="attempt_id"):
    """Replicates decision_state.start_decision_attempt's real sequence:
    lock applications, recheck funded/manual_reviews, recheck/expire a
    stale in_progress attempt, then insert a fresh one."""
    conn = _new_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SET search_path TO {SCHEMA}")
            cur.execute("SELECT status FROM applications WHERE id = %s FOR UPDATE", (app_id,))
            status_rows = cur.fetchall()
            if not status_rows:
                result["outcome"] = "not_found"
                conn.rollback()
                return
            if status_rows[0]["status"] == "funded":
                result["outcome"] = "blocked_funded"
                conn.rollback()
                return
            cur.execute("SELECT 1 FROM manual_reviews WHERE app_id = %s", (app_id,))
            if cur.fetchall():
                result["outcome"] = "blocked_manual"
                conn.rollback()
                return

            cur.execute(
                "SELECT id, (lease_expires_at > now()) AS live "
                "FROM decision_attempts WHERE app_id = %s AND state = 'in_progress' FOR UPDATE",
                (app_id,),
            )
            existing = cur.fetchall()
            if existing:
                if existing[0]["live"]:
                    result["outcome"] = "blocked_live_attempt"
                    conn.rollback()
                    return
                cur.execute(
                    "UPDATE decision_attempts SET state = 'expired', completed_at = now(), "
                    "failure_code = 'expired_lease', failure_detail = 'lease exceeded' WHERE id = %s",
                    (existing[0]["id"],),
                )
                result["recovered_stale_attempt_id"] = existing[0]["id"]

            cur.execute(
                "INSERT INTO decision_attempts (app_id, state, requested_by, lease_expires_at) "
                "VALUES (%s, 'in_progress', 'borrower', now() + (%s || ' seconds')::interval) "
                "RETURNING id",
                (app_id, lease_seconds),
            )
            result[key] = cur.fetchall()[0]["id"]
        conn.commit()
        result["outcome"] = result.get("outcome", "started")
    finally:
        conn.close()


# --- TXN B: run_decision's post-call recheck + persist, real sequence ------

def _finalize_attempt(app_id, attempt_id, outcome, result):
    """Replicates run_decision's TXN B: lock again, FIRST verify this
    attempt is still the live/active reservation (state='in_progress' AND
    lease not passed -- the lease invariant), then recheck finality, and
    only write decisions + decision_events + mark completed on the winning
    branch -- a discarded/inactive attempt writes neither."""
    conn = _new_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SET search_path TO {SCHEMA}")

            cur.execute(
                "SELECT state, (lease_expires_at > now()) AS live "
                "FROM decision_attempts WHERE id = %s FOR UPDATE",
                (attempt_id,),
            )
            attempt_rows = cur.fetchall()
            still_active = bool(attempt_rows) and attempt_rows[0]["state"] == "in_progress" and attempt_rows[0]["live"]
            if not still_active:
                if attempt_rows and attempt_rows[0]["state"] == "in_progress" and not attempt_rows[0]["live"]:
                    cur.execute(
                        "UPDATE decision_attempts SET state = 'expired', completed_at = now(), "
                        "failure_code = 'expired_lease', failure_detail = 'discovered in TXN B' "
                        "WHERE id = %s AND state = 'in_progress'",
                        (attempt_id,),
                    )
                result["outcome"] = "discarded_attempt_inactive"
                conn.commit()
                return

            cur.execute("SELECT status FROM applications WHERE id = %s FOR UPDATE", (app_id,))
            funded = cur.fetchall()[0]["status"] == "funded"
            cur.execute("SELECT outcome, reason, reviewer_role, reviewed_at FROM manual_reviews WHERE app_id = %s", (app_id,))
            manual = cur.fetchall()

            if funded:
                cur.execute(
                    "UPDATE decision_attempts SET state = 'discarded', completed_at = now(), "
                    "failure_code = 'funded', failure_detail = 'funded before persistence' WHERE id = %s",
                    (attempt_id,),
                )
                result["outcome"] = "discarded_funded"
            elif manual:
                cur.execute(
                    "UPDATE decision_attempts SET state = 'discarded', completed_at = now(), "
                    "failure_code = 'superseded_by_staff', failure_detail = 'staff decided first' WHERE id = %s",
                    (attempt_id,),
                )
                result["outcome"] = "discarded_superseded"
            else:
                cur.execute(
                    "INSERT INTO decisions (app_id, outcome) VALUES (%s, %s) "
                    "ON CONFLICT (app_id) DO UPDATE SET outcome = EXCLUDED.outcome",
                    (app_id, outcome),
                )
                cur.execute(
                    "INSERT INTO decision_events (app_id, model_version, decision, attempt_id) "
                    "VALUES (%s, 'v1', %s, %s)",
                    (app_id, outcome, attempt_id),
                )
                cur.execute(
                    "UPDATE decision_attempts SET state = 'completed', completed_at = now() WHERE id = %s",
                    (attempt_id,),
                )
                result["outcome"] = "completed"
        conn.commit()
    finally:
        conn.close()


def _staff_resolves_refer(app_id, outcome, reason, result):
    conn = _new_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SET search_path TO {SCHEMA}")
            cur.execute("SELECT status FROM applications WHERE id = %s FOR UPDATE", (app_id,))
            cur.fetchall()
            cur.execute(
                "INSERT INTO manual_reviews (app_id, reviewer_role, outcome, reason) "
                "VALUES (%s, 'underwriter', %s, %s) ON CONFLICT (app_id) DO NOTHING RETURNING outcome",
                (app_id, outcome, reason),
            )
            won = cur.fetchall()
        conn.commit()
        result["outcome"] = "won" if won else "lost_race"
    finally:
        conn.close()


def test_manual_finality_before_rerun_blocks_before_any_attempt_is_created(setup_conn):
    """Requirement: manual finality before rerun -- zero decision-service
    calls and zero new events. Proven here as zero attempt rows and zero
    decision_events rows ever created at all."""
    _seed_app(setup_conn, 1)
    with setup_conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(
            "INSERT INTO manual_reviews (app_id, reviewer_role, outcome, reason) "
            "VALUES (1, 'underwriter', 'deny', 'DTI too high')"
        )
    setup_conn.commit()

    result = {}
    _start_attempt(1, 60, result)

    assert result["outcome"] == "blocked_manual"
    with setup_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT count(*) AS n FROM decision_attempts")
        assert cur.fetchone()["n"] == 0
        cur.execute("SELECT count(*) AS n FROM decision_events")
        assert cur.fetchone()["n"] == 0


def test_live_lease_blocks_a_second_rerun(setup_conn):
    """Requirement: non-expired attempt blocks a second rerun without a
    bureau call."""
    _seed_app(setup_conn, 2)
    first = {}
    _start_attempt(2, 60, first)
    assert first["outcome"] == "started"

    second = {}
    _start_attempt(2, 60, second)
    assert second["outcome"] == "blocked_live_attempt"

    with setup_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT count(*) AS n FROM decision_attempts WHERE app_id = 2")
        assert cur.fetchone()["n"] == 1, "the blocked second call must not have created its own attempt row"


def test_stale_lease_is_atomically_expired_and_replaced(setup_conn):
    """Requirement: crash/process loss after TXN A; lease expires;
    subsequent attempt succeeds. A 1-second lease, then a real sleep past
    it, simulates a lease that has since expired (the process that created
    it crashed before ever completing it) -- the next request must
    atomically terminalize it and create a fresh one, under the same lock.
    (A negative/zero lease isn't used here -- decision_attempts_lease_
    after_start requires lease_expires_at > started_at at insert time,
    correctly rejecting a lease that was never even momentarily valid.)"""
    _seed_app(setup_conn, 3)
    crashed = {}
    _start_attempt(3, 1, crashed)
    assert crashed["outcome"] == "started"
    stale_id = crashed["attempt_id"]
    time.sleep(1.2)

    recovered = {}
    _start_attempt(3, 60, recovered)

    assert recovered["outcome"] == "started"
    assert recovered["recovered_stale_attempt_id"] == stale_id
    assert recovered["attempt_id"] != stale_id

    with setup_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT state, failure_code FROM decision_attempts WHERE id = %s", (stale_id,))
        row = cur.fetchone()
        assert row["state"] == "expired"
        assert row["failure_code"] == "expired_lease"
        cur.execute("SELECT state FROM decision_attempts WHERE id = %s", (recovered["attempt_id"],))
        assert cur.fetchone()["state"] == "in_progress"
        cur.execute("SELECT count(*) AS n FROM decision_attempts WHERE app_id = 3 AND state = 'in_progress'")
        assert cur.fetchone()["n"] == 1, "exactly one in_progress attempt after recovery, never two"


def test_staff_wins_during_the_external_call_attempt_discarded_no_permanent_event(setup_conn):
    """The core Finding 2 proof: staff resolves the refer WHILE the
    (simulated) external call to decision-service is in flight. The
    attempt must discard, and -- unlike the rejected pre-call/post-call
    design -- write NO decision_events row at all, not a misleading one."""
    _seed_app(setup_conn, 4)
    started = {}
    _start_attempt(4, 60, started)
    attempt_id = started["attempt_id"]

    barrier = threading.Barrier(2)
    staff_result, finalize_result = {}, {}

    def _staff():
        barrier.wait(timeout=5)
        _staff_resolves_refer(4, "deny", "manual re-verification", staff_result)

    def _automated_finalize_after_the_call():
        barrier.wait(timeout=5)
        time.sleep(0.2)  # the "external call" finishing after staff wins
        _finalize_attempt(4, attempt_id, "approve", finalize_result)

    t1 = threading.Thread(target=_staff)
    t2 = threading.Thread(target=_automated_finalize_after_the_call)
    t1.start(); t2.start()
    t1.join(timeout=10); t2.join(timeout=10)

    assert staff_result["outcome"] == "won"
    assert finalize_result["outcome"] == "discarded_superseded"

    with setup_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT outcome FROM decisions WHERE app_id = 4")
        assert cur.fetchall() == [], "no decisions row may exist -- staff's own review IS the decision here"
        cur.execute("SELECT count(*) AS n FROM decision_events WHERE app_id = 4")
        assert cur.fetchone()["n"] == 0, "a discarded attempt must write ZERO decision_events rows"
        cur.execute("SELECT state, failure_code FROM decision_attempts WHERE id = %s", (attempt_id,))
        row = cur.fetchone()
        assert row["state"] == "discarded"
        assert row["failure_code"] == "superseded_by_staff"


def test_attempt_completes_first_decision_and_event_commit_atomically(setup_conn):
    """Requirement: automated attempt completes first -- decision and event
    commit atomically (and, on this branch, the attempt is marked
    completed in the very same transaction)."""
    _seed_app(setup_conn, 5)
    started = {}
    _start_attempt(5, 60, started)
    attempt_id = started["attempt_id"]

    result = {}
    _finalize_attempt(5, attempt_id, "approve", result)

    assert result["outcome"] == "completed"
    with setup_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT outcome FROM decisions WHERE app_id = 5")
        assert cur.fetchone()["outcome"] == "approve"
        cur.execute("SELECT decision, attempt_id FROM decision_events WHERE app_id = 5")
        row = cur.fetchone()
        assert row["decision"] == "approve"
        assert row["attempt_id"] == attempt_id
        cur.execute("SELECT state FROM decision_attempts WHERE id = %s", (attempt_id,))
        assert cur.fetchone()["state"] == "completed"


def test_late_result_after_lease_expiry_and_replacement_does_not_commit(setup_conn):
    """Required lease invariant: the first computation returns AFTER its own
    attempt's lease has expired and a replacement attempt already exists
    (and has already completed) -- the first (late) result must be
    discarded in TXN B, writing no decisions/decision_events, and must
    never overwrite what the replacement attempt already committed."""
    _seed_app(setup_conn, 9)

    # Attempt A starts with a 1-second lease -- simulates a request whose
    # external call is about to take far longer than that.
    first = {}
    _start_attempt(9, 1, first)
    assert first["outcome"] == "started"
    attempt_a = first["attempt_id"]

    time.sleep(1.2)  # A's lease has now genuinely expired

    # A second request comes in (e.g. the borrower retries), recovers A
    # (marks it 'expired') and starts a fresh attempt B.
    second = {}
    _start_attempt(9, 60, second)
    assert second["outcome"] == "started"
    assert second["recovered_stale_attempt_id"] == attempt_a
    attempt_b = second["attempt_id"]
    assert attempt_b != attempt_a

    # B's (faster) computation finishes first and completes normally.
    result_b = {}
    _finalize_attempt(9, attempt_b, "approve", result_b)
    assert result_b["outcome"] == "completed"

    # NOW A's original, slow computation finally returns and reaches its
    # own TXN B -- A is no longer 'in_progress' (it's 'expired'), so this
    # must be discarded, not committed, regardless of what outcome it
    # carries.
    result_a = {}
    _finalize_attempt(9, attempt_a, "deny", result_a)
    assert result_a["outcome"] == "discarded_attempt_inactive"

    with setup_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        # The decisions row must still reflect B's result -- A's late
        # 'deny' must never have landed, whether as an overwrite or
        # otherwise.
        cur.execute("SELECT outcome FROM decisions WHERE app_id = 9")
        assert cur.fetchone()["outcome"] == "approve"
        # Exactly one decision_events row -- B's -- never a second one from A.
        cur.execute("SELECT decision, attempt_id FROM decision_events WHERE app_id = 9")
        rows = cur.fetchall()
        assert len(rows) == 1
        assert rows[0]["attempt_id"] == attempt_b
        assert rows[0]["decision"] == "approve"
        # A's own row is untouched by its late arrival -- still 'expired'
        # from the earlier recovery, not silently flipped back to
        # 'completed'.
        cur.execute("SELECT state FROM decision_attempts WHERE id = %s", (attempt_a,))
        assert cur.fetchone()["state"] == "expired"


def test_two_simultaneous_reruns_have_one_deterministic_winner(setup_conn):
    """Requirement: two simultaneous reruns have one deterministic winner --
    the applications row lock serializes TXN A itself, so only one of two
    concurrent start_decision_attempt calls may ever succeed; the loser is
    rejected before any bureau/model work, not after a duplicate one ran."""
    _seed_app(setup_conn, 6)
    barrier = threading.Barrier(2)
    result_a, result_b = {}, {}

    def _attempt(result):
        # Replicate start_decision_attempt but pause at the barrier BEFORE
        # taking the lock, so both threads race to acquire it.
        conn = _new_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(f"SET search_path TO {SCHEMA}")
                barrier.wait(timeout=5)
                cur.execute("SELECT status FROM applications WHERE id = %s FOR UPDATE", (6,))
                cur.fetchall()
                cur.execute(
                    "SELECT id, (lease_expires_at > now()) AS live FROM decision_attempts "
                    "WHERE app_id = %s AND state = 'in_progress' FOR UPDATE",
                    (6,),
                )
                existing = cur.fetchall()
                if existing and existing[0]["live"]:
                    result["outcome"] = "blocked_live_attempt"
                    conn.rollback()
                    return
                cur.execute(
                    "INSERT INTO decision_attempts (app_id, state, requested_by, lease_expires_at) "
                    "VALUES (%s, 'in_progress', 'borrower', now() + interval '60 seconds') RETURNING id",
                    (6,),
                )
                result["attempt_id"] = cur.fetchall()[0]["id"]
            conn.commit()
            result["outcome"] = "started"
        finally:
            conn.close()

    t1 = threading.Thread(target=_attempt, args=(result_a,))
    t2 = threading.Thread(target=_attempt, args=(result_b,))
    t1.start(); t2.start()
    t1.join(timeout=10); t2.join(timeout=10)

    outcomes = {result_a["outcome"], result_b["outcome"]}
    assert outcomes == {"started", "blocked_live_attempt"}, (result_a, result_b)

    with setup_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT count(*) AS n FROM decision_attempts WHERE app_id = 6")
        assert cur.fetchone()["n"] == 1, "exactly one attempt row -- the second thread's INSERT must never run"


def _backends_holding_open_transactions_on_this_schema(conn) -> int:
    """How many OTHER backends sit `idle in transaction` holding a lock on this
    test schema's relations.

    The scoping is the whole point. This check used to count every
    `idle in transaction` backend in the database:

        SELECT count(*) FROM pg_stat_activity
         WHERE state = 'idle in transaction' AND datname = current_database()

    Nothing tied that to the attempt under test, so any unrelated client idling
    in a transaction failed it -- and a running `docker compose` stack has
    several. The test passed in CI (where the stack is down during the
    db-migrations job) and failed locally for anyone with the application
    running, which is the worst combination: a red test that says nothing about
    the code and cannot be reproduced by the job that is green.

    What the requirement actually says is narrower and checkable: after TXN A
    commits, no connection is left holding a transaction against the rows this
    attempt touched. So the query joins `pg_locks` and asks only about locks on
    `applications` and `decision_attempts` IN THIS SCHEMA, excluding our own
    backend.

    `to_regclass` rather than a name comparison: it resolves the schema-qualified
    name to the oid `pg_locks.relation` actually holds, and returns NULL instead
    of raising if the relation is gone.
    """
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT count(DISTINCT a.pid) AS n "
            "  FROM pg_stat_activity a "
            "  JOIN pg_locks l ON l.pid = a.pid "
            " WHERE a.state = 'idle in transaction' "
            "   AND a.datname = current_database() "
            "   AND a.pid <> pg_backend_pid() "
            "   AND l.relation IN (to_regclass(%s), to_regclass(%s))",
            (f"{SCHEMA}.applications", f"{SCHEMA}.decision_attempts"),
        )
        return cur.fetchone()["n"]


def test_no_open_transaction_during_the_external_call(setup_conn):
    """Requirement: no database transaction is open during the external
    HTTP call. Proven directly: after start_decision_attempt's TXN A
    commits, query pg_stat_activity for any OTHER backend still holding an
    open transaction against this schema's connection -- there must be
    none, because TXN A's connection is closed before any network call is
    ever made (see decision_state.start_decision_attempt: `with
    db.transaction()` closes its own connection on exit, and the actual
    external call happens strictly after that `with` block, outside any
    transaction)."""
    _seed_app(setup_conn, 7)
    started = {}
    _start_attempt(7, 60, started)
    assert started["outcome"] == "started"

    # Simulate "the external call is now in flight" -- a real gap where no
    # transaction related to this attempt may be open.
    #
    # Scoped to locks on THIS schema's relations rather than to every
    # transaction in the database; see the helper for why the unscoped version
    # failed on any machine with the application running.
    assert _backends_holding_open_transactions_on_this_schema(setup_conn) == 0, (
        "a connection is left holding an open transaction on this attempt's "
        "rows after TXN A committed")


def test_the_open_transaction_check_still_detects_a_real_leak(setup_conn):
    """Guard the guard, and this one earns its place.

    Narrowing a check is how a check becomes vacuous: the previous version failed
    on unrelated traffic, and the obvious repair -- scoping it -- could just as
    easily have scoped it to nothing and passed forever. So this plants exactly
    the leak the requirement forbids: a second connection that locks
    `applications` and then sits there without committing, which is what a
    network call made inside TXN A would look like from the database's side.

    The check must see it. If this test ever passes with the assertion below
    reversed, the scoping has gone too far.
    """
    _seed_app(setup_conn, 21)

    leaked = _new_conn()
    try:
        with leaked.cursor() as cur:
            cur.execute(f"SET search_path TO {SCHEMA}")
            # FOR UPDATE, then no commit -- the connection goes idle holding the
            # row lock, exactly the state TXN A must not be in during a call out.
            cur.execute("SELECT id FROM applications WHERE id = 21 FOR UPDATE")
            cur.fetchall()

        # The leaked backend has to have reached `idle in transaction` before
        # pg_stat_activity reports it that way. Polled rather than slept once:
        # a fixed sleep is either flaky or slower than it needs to be.
        seen = 0
        for _ in range(50):
            seen = _backends_holding_open_transactions_on_this_schema(setup_conn)
            if seen:
                break
            time.sleep(0.05)

        assert seen == 1, (
            "the scoped check did not notice a connection idling in a "
            "transaction while holding a lock on this schema's applications row "
            "-- it has been narrowed until it proves nothing")
    finally:
        leaked.rollback()
        leaked.close()

    # And it goes back to zero once the leak is gone, so the check is reporting
    # the state rather than latching.
    for _ in range(50):
        if _backends_holding_open_transactions_on_this_schema(setup_conn) == 0:
            break
        time.sleep(0.05)
    assert _backends_holding_open_transactions_on_this_schema(setup_conn) == 0


def test_failed_external_call_releases_the_attempt_for_retry(setup_conn):
    """Requirement: computation failure marks attempt failed and releases
    locks -- a retry must be able to create a fresh attempt immediately,
    not be blocked by the failed one."""
    _seed_app(setup_conn, 8)
    started = {}
    _start_attempt(8, 60, started)
    attempt_id = started["attempt_id"]

    conn = _new_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {SCHEMA}")
            cur.execute(
                "UPDATE decision_attempts SET state = 'failed', completed_at = now(), "
                "failure_code = 'timeout', failure_detail = 'timed out' "
                "WHERE id = %s AND state = 'in_progress'",
                (attempt_id,),
            )
        conn.commit()
    finally:
        conn.close()

    retry = {}
    _start_attempt(8, 60, retry)
    assert retry["outcome"] == "started"
    assert retry["attempt_id"] != attempt_id

    with setup_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT state FROM decision_attempts WHERE id = %s", (attempt_id,))
        assert cur.fetchone()["state"] == "failed"
        cur.execute("SELECT state FROM decision_attempts WHERE id = %s", (retry["attempt_id"],))
        assert cur.fetchone()["state"] == "in_progress"
