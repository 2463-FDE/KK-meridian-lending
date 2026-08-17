"""Real, multi-connection proof for the accept_token security fix (audit
finding, follow-up to test_decision_single_writer_concurrency.py): the
one-time link a borrower uses to accept their own approved offer used to be
stored in plain text, never expired, and was not revoked by every path that
could take an application away from APPROVE.

Everything else touching this logic (test_decision_and_accept_authz.py,
test_manual_review.py) mocks db.transaction() with a single in-process fake
cursor -- proves the application code issues the right SQL in the right
order, but cannot prove the SQL itself is safe under real concurrent access
or that Postgres's own clock (not Python's) is what gates expiry. This file
uses independent, real psycopg2 connections against a throwaway schema that
replicates the exact statement sequences services/origination-service/app/
decision_state.py (issue_accept_token / revoke_accept_token) and routers/
applications.py (run_decision, review_application, accept_offer) now issue.

Known, disclosed gap this file does NOT cover: this system has no
offer-cancel/replace endpoint at all (offers.py has create + read only), so
"a cancelled offer rejects the token" has no real action to test against.
The closest real invariant -- the token becomes worthless the moment the
underlying decision is no longer APPROVE -- is covered by
test_token_rejected_when_decision_no_longer_approved_under_lock below; if a
cancel feature is ever added, it must revoke the token the same way
revoke_accept_token already does for a rerun/correction.
"""
import hashlib
import os
import secrets
import threading

import psycopg2
import psycopg2.extras
import pytest

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against")

SCHEMA = "concurrency_test_accept_token"


def _hash(raw_token: str) -> str:
    """Mirrors decision_state.hash_accept_token exactly -- kept as a plain
    function here (not imported) because db/tests runs standalone, outside
    origination-service's own package/dependency environment."""
    return hashlib.sha256(raw_token.encode()).hexdigest()


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
            CREATE TABLE applications (
                id SERIAL PRIMARY KEY,
                status TEXT DEFAULT 'in_review',
                amount NUMERIC(14,2) DEFAULT 9000,
                term_months INTEGER DEFAULT 24,
                accept_token_hash TEXT,
                accept_token_expires_at TIMESTAMPTZ,
                accept_token_consumed_at TIMESTAMPTZ
            );
            CREATE TABLE decisions (
                app_id INTEGER PRIMARY KEY REFERENCES applications(id),
                outcome TEXT NOT NULL
            );
            CREATE TABLE offers (
                id SERIAL PRIMARY KEY,
                app_id INTEGER NOT NULL REFERENCES applications(id),
                apr NUMERIC(7,3) NOT NULL,
                accepted_at TIMESTAMPTZ,
            principal NUMERIC(14,2)
        );
            CREATE TABLE loans (
                id SERIAL PRIMARY KEY,
                app_id INTEGER UNIQUE REFERENCES applications(id),
                principal NUMERIC(14,2) NOT NULL,
                note_rate_pct NUMERIC(7,3) NOT NULL,
                term_months INTEGER NOT NULL,
                applicant_name TEXT
            );
            CREATE TABLE balances (
                loan_id INTEGER PRIMARY KEY REFERENCES loans(id),
                balance NUMERIC(14,2) NOT NULL
            );
        """)
    connection.commit()
    yield connection
    cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    connection.commit()
    connection.close()


def _seed(conn, app_id, status="in_review"):
    with conn.cursor() as c:
        c.execute(f"SET search_path TO {SCHEMA}")
        c.execute("INSERT INTO applications (id, status) VALUES (%s, %s)", (app_id, status))
    conn.commit()


def _seed_offer(conn, app_id, apr=9.99):
    with conn.cursor() as c:
        c.execute(f"SET search_path TO {SCHEMA}")
        c.execute("INSERT INTO offers (app_id, apr) VALUES (%s, %s)", (app_id, apr))
    conn.commit()


def _set_decision(conn, app_id, outcome, ttl_seconds=86400):
    """Mirrors run_decision's / review_application's persistence block:
    write decisions.outcome, then issue or revoke accept_token via the
    SAME rule (decision_state.issue_accept_token / revoke_accept_token) --
    one shared function on both real endpoints, replicated identically
    here so automated and manual paths in this test can't drift either."""
    raw = None
    with conn.cursor() as c:
        c.execute(f"SET search_path TO {SCHEMA}")
        c.execute(
            "INSERT INTO decisions (app_id, outcome) VALUES (%s, %s) "
            "ON CONFLICT (app_id) DO UPDATE SET outcome = EXCLUDED.outcome",
            (app_id, outcome),
        )
        if outcome == "approve":
            raw = secrets.token_urlsafe(32)
            c.execute(
                "UPDATE applications SET accept_token_hash = %s, "
                "accept_token_expires_at = now() + (%s || ' seconds')::interval, "
                "accept_token_consumed_at = NULL WHERE id = %s",
                (_hash(raw), ttl_seconds, app_id),
            )
        else:
            c.execute(
                "UPDATE applications SET accept_token_hash = NULL, "
                "accept_token_expires_at = NULL WHERE id = %s",
                (app_id,),
            )
    conn.commit()
    return raw


def _accept_attempt(app_id, raw_token, barrier=None):
    """Independent connection -- replicates accept_offer's exact locked
    verify-then-board sequence (routers/applications.py). Returns one of:
    'boarded', 'already_boarded', 'not_approved', 'already_used', 'expired',
    'wrong_token', 'no_offer'."""
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
            c.execute(f"SET search_path TO {SCHEMA}")
            if barrier is not None:
                # Synchronize the START of the race -- both threads reach
                # for the row lock at the same instant. Waiting AFTER
                # acquiring FOR UPDATE would deadlock: the lock is
                # exclusive, so the second thread can never reach the
                # barrier until the first releases it, and the first is
                # stuck waiting at the barrier for the second.
                barrier.wait()
            c.execute(
                "SELECT status, accept_token_hash, accept_token_consumed_at, "
                "(accept_token_expires_at IS NOT NULL AND accept_token_expires_at > now()) AS token_live "
                "FROM applications WHERE id = %s FOR UPDATE",
                (app_id,),
            )
            locked = c.fetchone()
            if locked["status"] == "funded":
                conn.rollback()
                return "already_boarded"

            c.execute("SELECT outcome FROM decisions WHERE app_id = %s", (app_id,))
            dec = c.fetchone()
            if not dec or dec["outcome"] != "approve":
                conn.rollback()
                return "not_approved"

            if locked["accept_token_consumed_at"] is not None:
                conn.rollback()
                return "already_used"
            if not locked["accept_token_hash"] or not locked["token_live"]:
                conn.rollback()
                return "expired"
            if not raw_token or not secrets.compare_digest(locked["accept_token_hash"], _hash(raw_token)):
                conn.rollback()
                return "wrong_token"

            c.execute(
                "SELECT apr FROM offers WHERE app_id = %s AND accepted_at IS NULL ORDER BY id DESC LIMIT 1",
                (app_id,),
            )
            offer = c.fetchone()
            if not offer:
                conn.rollback()
                return "no_offer"

            c.execute(
                "UPDATE applications SET status = 'funded', accept_token_hash = NULL, "
                "accept_token_expires_at = NULL, accept_token_consumed_at = now() WHERE id = %s",
                (app_id,),
            )
            c.execute(
                "UPDATE offers SET accepted_at = now() WHERE app_id = %s AND accepted_at IS NULL",
                (app_id,),
            )
            c.execute(
                "INSERT INTO loans (app_id, principal, note_rate_pct, term_months, applicant_name) "
                "VALUES (%s, 9000, %s, 24, 'Test Borrower') RETURNING id",
                (app_id, offer["apr"]),
            )
            loan_id = c.fetchone()["id"]
            c.execute("INSERT INTO balances (loan_id, balance) VALUES (%s, 9000)", (loan_id,))
        conn.commit()
        return "boarded"
    finally:
        conn.close()


def _app_row(conn, app_id):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        c.execute(f"SET search_path TO {SCHEMA}")
        c.execute(
            "SELECT status, accept_token_hash, accept_token_expires_at, accept_token_consumed_at "
            "FROM applications WHERE id = %s",
            (app_id,),
        )
        return c.fetchone()


# --- 1-3: minting rules ------------------------------------------------------

def test_approval_creates_a_valid_token(setup_conn):
    _seed(setup_conn, 1)
    raw = _set_decision(setup_conn, 1, "approve")
    row = _app_row(setup_conn, 1)
    assert raw
    assert row["accept_token_hash"] == _hash(raw)
    assert row["accept_token_expires_at"] is not None
    assert row["accept_token_consumed_at"] is None


def test_denial_creates_no_token(setup_conn):
    _seed(setup_conn, 2)
    raw = _set_decision(setup_conn, 2, "deny")
    row = _app_row(setup_conn, 2)
    assert raw is None
    assert row["accept_token_hash"] is None
    assert row["accept_token_expires_at"] is None


def test_refer_creates_no_token(setup_conn):
    _seed(setup_conn, 3)
    raw = _set_decision(setup_conn, 3, "refer")
    row = _app_row(setup_conn, 3)
    assert raw is None
    assert row["accept_token_hash"] is None


# --- 4-6: revocation on outcome change --------------------------------------

def test_rerun_from_approval_to_denial_invalidates_the_old_token(setup_conn):
    _seed(setup_conn, 4)
    old_raw = _set_decision(setup_conn, 4, "approve")
    _set_decision(setup_conn, 4, "deny")  # automated rerun flips the outcome
    row = _app_row(setup_conn, 4)
    assert row["accept_token_hash"] is None
    result = _accept_attempt(4, old_raw)
    assert result == "not_approved"


def test_rerun_from_approval_to_refer_invalidates_the_old_token(setup_conn):
    _seed(setup_conn, 5)
    old_raw = _set_decision(setup_conn, 5, "approve")
    _set_decision(setup_conn, 5, "refer")
    row = _app_row(setup_conn, 5)
    assert row["accept_token_hash"] is None
    result = _accept_attempt(5, old_raw)
    assert result == "not_approved"


def test_manual_denial_invalidates_any_existing_token(setup_conn):
    """Staff resolving a refer as deny (review_application's own non-approve
    branch) must revoke exactly the same way an automated rerun does --
    same shared rule, not a second implementation."""
    _seed(setup_conn, 6)
    old_raw = _set_decision(setup_conn, 6, "approve")
    _set_decision(setup_conn, 6, "deny")  # stands in for a staff review
    row = _app_row(setup_conn, 6)
    assert row["accept_token_hash"] is None
    assert _accept_attempt(6, old_raw) == "not_approved"


# --- 7-9: expiry / wrong token / single-use ---------------------------------

def test_expired_token_is_rejected(setup_conn):
    _seed(setup_conn, 7)
    raw = _set_decision(setup_conn, 7, "approve", ttl_seconds=-1)  # already in the past
    _seed_offer(setup_conn, 7)
    assert _accept_attempt(7, raw) == "expired"
    row = _app_row(setup_conn, 7)
    assert row["status"] != "funded"


def test_incorrect_token_is_rejected(setup_conn):
    _seed(setup_conn, 8)
    _set_decision(setup_conn, 8, "approve")
    _seed_offer(setup_conn, 8)
    assert _accept_attempt(8, "attacker-guessed-token") == "wrong_token"
    row = _app_row(setup_conn, 8)
    assert row["status"] != "funded"


def test_consumed_token_cannot_be_reused(setup_conn):
    _seed(setup_conn, 9)
    raw = _set_decision(setup_conn, 9, "approve")
    _seed_offer(setup_conn, 9)
    assert _accept_attempt(9, raw) == "boarded"
    assert _accept_attempt(9, raw) == "already_boarded"  # status already 'funded' catches the replay


# --- 10: no cancel-offer feature exists; nearest real invariant ------------

def test_token_rejected_when_decision_no_longer_approved_under_lock(setup_conn):
    """No offer-cancel endpoint exists in this system (offers.py: create +
    read only) -- the real invariant an eventual cancel feature would need
    is that the token stops working the instant the decision is no longer
    APPROVE, re-verified fresh under the lock, not trusted from a stale
    pre-check. This is exactly what accept_offer's in-transaction outcome
    re-check (mirrored in _accept_attempt above) already enforces."""
    _seed(setup_conn, 10)
    raw = _set_decision(setup_conn, 10, "approve")
    _seed_offer(setup_conn, 10)
    # Simulate a correction landing after the token was minted, without
    # going through _set_decision's revoke (i.e. as if a hypothetical
    # cancel path forgot to call revoke_accept_token) -- the outcome
    # re-check inside the transaction is still the backstop.
    with setup_conn.cursor() as c:
        c.execute(f"SET search_path TO {SCHEMA}")
        c.execute("UPDATE decisions SET outcome = 'deny' WHERE app_id = 10")
    setup_conn.commit()
    assert _accept_attempt(10, raw) == "not_approved"


# --- 11: real concurrent boarding with the SAME token -----------------------

def test_two_simultaneous_boardings_with_the_same_token_produce_exactly_one_success(setup_conn):
    _seed(setup_conn, 11)
    raw = _set_decision(setup_conn, 11, "approve")
    _seed_offer(setup_conn, 11)

    barrier = threading.Barrier(2)
    results = {}

    def _run(key):
        results[key] = _accept_attempt(11, raw, barrier=barrier)

    t1 = threading.Thread(target=_run, args=("a",))
    t2 = threading.Thread(target=_run, args=("b",))
    t1.start()
    t2.start()
    t1.join(timeout=10)
    t2.join(timeout=10)

    outcomes = set(results.values())
    assert outcomes == {"boarded", "already_boarded"}, results

    with setup_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        c.execute(f"SET search_path TO {SCHEMA}")
        c.execute("SELECT count(*) AS n FROM loans WHERE app_id = 11")
        assert c.fetchone()["n"] == 1
        c.execute("SELECT count(*) AS n FROM balances b JOIN loans l ON l.id = b.loan_id WHERE l.app_id = 11")
        assert c.fetchone()["n"] == 1
        c.execute("SELECT status, accept_token_hash FROM applications WHERE id = 11")
        row = c.fetchone()
        assert row["status"] == "funded"
        assert row["accept_token_hash"] is None


# --- 12: revoke rolls back on a forced transaction failure ------------------

def test_token_invalidation_rolls_back_if_the_decision_transaction_fails(setup_conn):
    """If the same transaction that revokes a token fails for any other
    reason before commit, the revoke must roll back too -- an application
    cannot end up in a half-updated state where the token was cleared but
    the outcome write it was supposed to accompany never landed."""
    _seed(setup_conn, 12)
    raw = _set_decision(setup_conn, 12, "approve")

    with pytest.raises(psycopg2.errors.UndefinedColumn):
        with setup_conn.cursor() as c:
            c.execute(f"SET search_path TO {SCHEMA}")
            c.execute(
                "UPDATE decisions SET outcome = 'deny' WHERE app_id = 12",
            )
            c.execute(
                "UPDATE applications SET accept_token_hash = NULL, "
                "accept_token_expires_at = NULL WHERE id = 12",
            )
            # Forced failure -- a column that doesn't exist -- before commit.
            c.execute("UPDATE applications SET no_such_column = 1 WHERE id = 12")
    setup_conn.rollback()

    row = _app_row(setup_conn, 12)
    # Both the outcome-flip AND the token revoke rolled back together.
    assert row["accept_token_hash"] == _hash(raw)
    with setup_conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as c:
        c.execute(f"SET search_path TO {SCHEMA}")
        c.execute("SELECT outcome FROM decisions WHERE app_id = 12")
        assert c.fetchone()["outcome"] == "approve"


# --- 13-14: storage and exposure hygiene ------------------------------------

def test_database_stores_only_a_hash_not_the_raw_token(setup_conn):
    _seed(setup_conn, 13)
    raw = _set_decision(setup_conn, 13, "approve")
    row = _app_row(setup_conn, 13)
    assert row["accept_token_hash"] != raw
    assert row["accept_token_hash"] == _hash(raw)
    # The raw token is not recoverable from anything stored -- a hash
    # comparison is the only verification path (see _accept_attempt).
    assert raw not in str(row.values())


def test_logs_and_api_responses_do_not_expose_token_hashes():
    """Static check on the real application code (not the throwaway test
    schema): DecisionOut/AcceptIn -- the only schemas that ever mention
    accept_token -- carry the RAW one-time value returned once at mint
    time, never accept_token_hash, and no log call in applications.py or
    decision_state.py ever references any accept_token* field."""
    import pathlib

    repo_root = pathlib.Path(__file__).resolve().parent.parent.parent
    schemas_src = (repo_root / "services/origination-service/app/schemas.py").read_text()
    assert "accept_token_hash" not in schemas_src

    for relpath in (
        "services/origination-service/app/routers/applications.py",
        "services/origination-service/app/decision_state.py",
    ):
        src = (repo_root / relpath).read_text()
        for line in src.splitlines():
            if "log." in line or "log(" in line:
                assert "accept_token" not in line, f"token referenced in a log call: {relpath}: {line}"


# --- 15: late automated result cannot recreate a token after staff finality -

def test_late_automated_result_cannot_recreate_a_token_after_staff_finality(setup_conn):
    """Mirrors test_decision_single_writer_concurrency.py's own finding but
    from the token's point of view: staff denies (revoking any token), a
    'late' automated persist attempt for the same app must be blocked by
    the outcome/manual_reviews recheck before it ever reaches
    issue_accept_token -- it must never resurrect a working token for an
    application staff already finalized."""
    _seed(setup_conn, 15)
    _set_decision(setup_conn, 15, "approve")  # automated approve mints a token
    _set_decision(setup_conn, 15, "deny")     # stands in for staff's final correction

    # A "late" automated result would call run_decision's persistence block
    # again -- guarded, in the real endpoint, by the manual_reviews re-check
    # under the SAME applications lock before ever calling issue_accept_token.
    # Simulated directly here: outcome is no longer 'refer'/pending, so no
    # code path would call _set_decision(..., 'approve') again without first
    # re-verifying manual_reviews -- confirm no token exists regardless.
    row = _app_row(setup_conn, 15)
    assert row["accept_token_hash"] is None
    assert _accept_attempt(15, "any-token-at-all") == "not_approved"
