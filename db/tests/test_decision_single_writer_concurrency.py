"""Real, multi-connection concurrency proof for the single-authoritative-
writer architecture (Phase 1 of the audit-driven fix).

Everything else testing this logic (test_manual_review.py,
test_decision_and_accept_authz.py) mocks db.transaction() with a single
in-process fake cursor -- that proves the application code issues the
right SQL in the right order, but it CANNOT prove the SQL itself is safe
under real concurrent access, because there's only ever one (fake)
connection involved. This file uses two independent, real psycopg2
connections, synchronized with a threading.Barrier so both actually
contend for the same Postgres row lock, replicating the exact statement
sequences services/origination-service/app/routers/applications.py now
issues for review_application (resolve a refer) and run_decision's
persistence step (the single authoritative write to `decisions`).

Architecture under test: decision-service no longer writes `decisions`
itself (see services/decision-service/app/graph.py::_node_persist) --
origination-service is the sole writer, and both of its own writers
(review_application, run_decision's persistence step) lock the SAME
applications row (FOR UPDATE) before touching decisions/manual_reviews.
"""
import os
import threading

import psycopg2
import psycopg2.extras
import pytest

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against")

SCHEMA = "concurrency_test_single_writer"


@pytest.fixture
def setup_conn():
    """A dedicated connection used only to create/seed/inspect/tear down the
    schema -- never used for the actual concurrent contention under test."""
    connection = psycopg2.connect(DATABASE_URL)
    connection.autocommit = False
    cur = connection.cursor()
    cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    cur.execute(f"CREATE SCHEMA {SCHEMA}")
    connection.commit()
    with connection.cursor() as c:
        c.execute(f"""
            SET search_path TO {SCHEMA};
            CREATE TABLE applications (id SERIAL PRIMARY KEY, status TEXT DEFAULT 'in_review');
            CREATE TABLE decisions (app_id INTEGER PRIMARY KEY REFERENCES applications(id), outcome TEXT NOT NULL);
            CREATE TABLE manual_reviews (
                id SERIAL PRIMARY KEY,
                app_id INTEGER NOT NULL REFERENCES applications(id) UNIQUE,
                reviewer_role TEXT NOT NULL,
                outcome TEXT NOT NULL,
                reason TEXT NOT NULL,
                reviewed_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
    connection.commit()
    yield connection
    cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    connection.commit()
    connection.close()


def _seed(conn, app_id, outcome="refer", status="in_review"):
    with conn.cursor() as c:
        c.execute(f"SET search_path TO {SCHEMA}")
        c.execute("INSERT INTO applications (id, status) VALUES (%s, %s)", (app_id, status))
        c.execute("INSERT INTO decisions (app_id, outcome) VALUES (%s, %s)", (app_id, outcome))
    conn.commit()


def _new_conn():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    return conn


def _review_application_attempt(app_id, reviewer_role, outcome, reason, barrier, result):
    """Replicates review_application's real statement sequence exactly:
    lock applications, re-read decisions.outcome, require 'refer', atomic
    INSERT ... ON CONFLICT DO NOTHING onto manual_reviews, then (only if it
    won) UPDATE decisions + applications."""
    conn = _new_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SET search_path TO {SCHEMA}")
            barrier.wait(timeout=5)
            cur.execute("SELECT status FROM applications WHERE id = %s FOR UPDATE", (app_id,))
            status = cur.fetchall()[0]["status"]
            if status == "funded":
                result["outcome"] = "blocked_funded"
                conn.rollback()
                return

            cur.execute("SELECT outcome FROM decisions WHERE app_id = %s", (app_id,))
            current_outcome = cur.fetchall()[0]["outcome"]
            if current_outcome != "refer":
                result["outcome"] = "blocked_not_refer"
                result["seen_outcome"] = current_outcome
                conn.rollback()
                return

            cur.execute(
                "INSERT INTO manual_reviews (app_id, reviewer_role, outcome, reason) "
                "VALUES (%s, %s, %s, %s) ON CONFLICT (app_id) DO NOTHING "
                "RETURNING outcome, reason, reviewer_role",
                (app_id, reviewer_role, outcome, reason),
            )
            won = cur.fetchall()
            if not won:
                cur.execute(
                    "SELECT outcome, reason, reviewer_role FROM manual_reviews WHERE app_id = %s",
                    (app_id,),
                )
                result["outcome"] = "lost_race"
                result["winner"] = cur.fetchall()[0]
                conn.rollback()
                return

            cur.execute("UPDATE decisions SET outcome = %s WHERE app_id = %s", (outcome, app_id))
            cur.execute("UPDATE applications SET status = %s WHERE id = %s", (outcome, app_id))
        conn.commit()
        result["outcome"] = "won"
        result["reviewer_role"] = reviewer_role
    finally:
        conn.close()


def _automated_persist_attempt(app_id, proposed_outcome, result):
    """Replicates run_decision's real persistence sequence: lock
    applications, re-check manual_reviews, and only write `decisions` if
    none exists."""
    conn = _new_conn()
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SET search_path TO {SCHEMA}")
            cur.execute("SELECT status FROM applications WHERE id = %s FOR UPDATE", (app_id,))
            status = cur.fetchall()[0]["status"]
            if status == "funded":
                result["outcome"] = "blocked_funded"
                conn.rollback()
                return

            cur.execute(
                "SELECT outcome, reason, reviewer_role FROM manual_reviews WHERE app_id = %s",
                (app_id,),
            )
            manual = cur.fetchall()
            if manual:
                result["outcome"] = "discarded_manual_exists"
                result["manual"] = manual[0]
                conn.rollback()
                return

            cur.execute(
                "INSERT INTO decisions (app_id, outcome) VALUES (%s, %s) "
                "ON CONFLICT (app_id) DO UPDATE SET outcome = EXCLUDED.outcome",
                (app_id, proposed_outcome),
            )
            cur.execute("UPDATE applications SET status = %s WHERE id = %s", (proposed_outcome, app_id))
        conn.commit()
        result["outcome"] = "persisted"
    finally:
        conn.close()


def test_two_simultaneous_staff_decisions_exactly_one_commits(setup_conn):
    """Requirement: two simultaneous staff decisions -- exactly one commits.

    Empirical finding (this test is what discovered it, run against real
    Postgres, not assumed): the SECOND thread to acquire the applications
    row lock is rejected by the outcome-recheck ("blocked_not_refer"), NOT
    by manual_reviews' own ON CONFLICT DO NOTHING ("lost_race"). Because
    the winner updates decisions.outcome away from 'refer' in the SAME
    transaction as its manual_reviews insert, before releasing the lock,
    the second thread can never observe outcome='refer' once it finally
    gets the lock -- so it never even reaches the manual_reviews INSERT.
    The ON CONFLICT constraint is still a real, necessary backstop (e.g.
    for any future code path that reaches the insert without holding this
    same lock), but for THIS exact race, the outcome-recheck is what
    actually fires first."""
    _seed(setup_conn, app_id=1, outcome="refer")
    barrier = threading.Barrier(2)
    result_a, result_b = {}, {}

    t1 = threading.Thread(target=_review_application_attempt,
                           args=(1, "underwriter", "approve", "DTI ok", barrier, result_a))
    t2 = threading.Thread(target=_review_application_attempt,
                           args=(1, "csr", "deny", "changed mind", barrier, result_b))
    t1.start(); t2.start()
    t1.join(timeout=10); t2.join(timeout=10)

    outcomes = {result_a["outcome"], result_b["outcome"]}
    assert outcomes == {"won", "blocked_not_refer"}, (result_a, result_b)
    winner = result_a if result_a["outcome"] == "won" else result_b
    loser = result_b if result_a["outcome"] == "won" else result_a

    # The loser's observed outcome must match what the winner actually committed.
    with setup_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT outcome, reason, reviewer_role FROM manual_reviews WHERE app_id = 1")
        rows = cur.fetchall()
    assert len(rows) == 1, "exactly one manual_reviews row must exist -- not zero, not two"
    assert rows[0]["reviewer_role"] == winner["reviewer_role"]
    assert loser["seen_outcome"] == rows[0]["outcome"]


def test_manual_reviews_on_conflict_is_the_backstop_when_the_lock_itself_is_skipped(setup_conn):
    """The ON CONFLICT DO NOTHING constraint on manual_reviews.app_id is
    not exercised as the primary guard by the test above (the
    outcome-recheck fires first) -- this proves it independently still
    works as a backstop for any writer that reaches the INSERT without
    going through the lock+recheck sequence at all (e.g. a future code
    path, or a bug that skips the SELECT ... FOR UPDATE)."""
    _seed(setup_conn, app_id=5, outcome="refer")
    conn_a, conn_b = _new_conn(), _new_conn()
    try:
        for conn in (conn_a, conn_b):
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {SCHEMA}")
        with conn_a.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO manual_reviews (app_id, reviewer_role, outcome, reason) "
                "VALUES (5, 'underwriter', 'approve', 'first') "
                "ON CONFLICT (app_id) DO NOTHING RETURNING outcome",
            )
            assert cur.fetchall()
        conn_a.commit()

        with conn_b.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "INSERT INTO manual_reviews (app_id, reviewer_role, outcome, reason) "
                "VALUES (5, 'csr', 'deny', 'second, no lock taken') "
                "ON CONFLICT (app_id) DO NOTHING RETURNING outcome",
            )
            assert cur.fetchall() == [], "the second insert must be silently rejected by the UNIQUE constraint"
        conn_b.commit()
    finally:
        conn_a.close()
        conn_b.close()

    with setup_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT reviewer_role FROM manual_reviews WHERE app_id = 5")
        rows = cur.fetchall()
    assert len(rows) == 1
    assert rows[0]["reviewer_role"] == "underwriter"


def test_automated_persist_discards_when_staff_commits_first(setup_conn):
    """Requirement: staff decision commits while automated scoring is 'in
    flight' -- when the automated persistence step finally runs (after
    decision-service has already responded, per the real code's ordering),
    it must discard its own result rather than overwrite the staff
    decision, and the DB must show the reviewer's decision, never the
    model's."""
    _seed(setup_conn, app_id=2, outcome="refer")
    barrier = threading.Barrier(2)
    staff_result, auto_result = {}, {}

    def _staff_then_release():
        _review_application_attempt(2, "underwriter", "approve", "DTI ok", barrier, staff_result)

    def _automated_waits_then_runs():
        barrier.wait(timeout=5)
        # Give the staff transaction a head start to acquire the lock and
        # commit -- this reproduces "automated result is ready, but staff
        # commits before persistence".
        import time
        time.sleep(0.2)
        _automated_persist_attempt(2, "deny", auto_result)

    t1 = threading.Thread(target=_staff_then_release)
    t2 = threading.Thread(target=_automated_waits_then_runs)
    t1.start(); t2.start()
    t1.join(timeout=10); t2.join(timeout=10)

    assert staff_result["outcome"] == "won"
    assert auto_result["outcome"] == "discarded_manual_exists"
    assert auto_result["manual"]["outcome"] == "approve"

    with setup_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT outcome FROM decisions WHERE app_id = 2")
        assert cur.fetchall()[0]["outcome"] == "approve", "the model's 'deny' must never have landed"


def test_manual_review_rejects_when_automated_persist_commits_first(setup_conn):
    """Requirement: automated approve/deny commits first -- the
    manual-review endpoint's own re-check (under the same lock) must see
    the outcome is no longer 'refer' and reject, never inserting a manual
    review against a decision that's already resolved."""
    _seed(setup_conn, app_id=3, outcome="refer")
    barrier = threading.Barrier(2)
    auto_result, staff_result = {}, {}

    def _automated_then_release():
        conn = _new_conn()
        try:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(f"SET search_path TO {SCHEMA}")
                barrier.wait(timeout=5)
                cur.execute("SELECT status FROM applications WHERE id = %s FOR UPDATE", (3,))
                cur.fetchall()
                cur.execute(
                    "INSERT INTO decisions (app_id, outcome) VALUES (%s, %s) "
                    "ON CONFLICT (app_id) DO UPDATE SET outcome = EXCLUDED.outcome",
                    (3, "approve"),
                )
                cur.execute("UPDATE applications SET status = %s WHERE id = %s", ("approve", 3))
            conn.commit()
            auto_result["outcome"] = "persisted"
        finally:
            conn.close()

    def _staff_waits_then_runs():
        barrier.wait(timeout=5)
        import time
        time.sleep(0.2)
        _review_application_attempt(3, "underwriter", "deny", "trying to resolve a refer", threading.Barrier(1), staff_result)

    t1 = threading.Thread(target=_automated_then_release)
    t2 = threading.Thread(target=_staff_waits_then_runs)
    t1.start(); t2.start()
    t1.join(timeout=10); t2.join(timeout=10)

    assert auto_result["outcome"] == "persisted"
    assert staff_result["outcome"] == "blocked_not_refer"
    assert staff_result["seen_outcome"] == "approve"

    with setup_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT * FROM manual_reviews WHERE app_id = 3")
        assert cur.fetchall() == [], "no manual review may exist for a non-refer outcome"


def test_no_direct_decision_write_path_can_overwrite_manual_finality(setup_conn):
    """Requirement: no direct write path can overwrite manual finality --
    even a caller that skips the row lock entirely (the one thing every
    other test in this file assumes is used correctly) is still stopped IF
    it goes through the ON CONFLICT-guarded manual_reviews table; this test
    documents the actual remaining exposure: decisions.outcome itself has
    no such guard at the table level -- only application code (the lock +
    recheck sequence exercised above) enforces it. A raw UPDATE issued by
    something that skips origination-service's own code path entirely
    (e.g. a bypassing direct-DB script) is NOT prevented by the schema."""
    _seed(setup_conn, app_id=4, outcome="refer")
    with setup_conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(
            "INSERT INTO manual_reviews (app_id, reviewer_role, outcome, reason) VALUES (%s, %s, %s, %s)",
            (4, "underwriter", "approve", "DTI ok"),
        )
    setup_conn.commit()

    # A raw UPDATE that bypasses all application code can still write
    # decisions.outcome -- this is the honest, documented residual gap:
    # finality is enforced by the lock+recheck sequence in application
    # code, not by a database constraint on decisions itself.
    with setup_conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("UPDATE decisions SET outcome = 'deny' WHERE app_id = 4")
    setup_conn.commit()

    with setup_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT outcome FROM decisions WHERE app_id = 4")
        # This assertion is INTENTIONALLY the "bad" outcome -- it documents,
        # rather than hides, that raw SQL bypassing origination-service's
        # own code path is not blocked by any database constraint today.
        assert cur.fetchall()[0]["outcome"] == "deny"
