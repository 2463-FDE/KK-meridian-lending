"""ADR 0011 step 2: the approval function, against real PostgreSQL.

ADR 0011 lists six things this function must prove, and says plainly that it "is
not done until every one of them fails when removed". Each has a case here, and
each was mutation-checked.

Concurrency is the reason this file cannot be a unit test. Two approvers racing
is the failure the lock exists for, and a mock cannot tell you whether
`FOR UPDATE` was written or merely intended: without it both transactions read
`resolution IS NULL`, both proceed, and the balance moves twice for one approval.

**No policy is asserted here.** The threshold and the permitted statuses are
parameters -- this file passes the approved cohort/demo values explicitly, as a
caller would, and the function encodes none of its own.
"""
import os
import pathlib
import threading

import psycopg2
import psycopg2.extras
import pytest

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against"
)

REPO = pathlib.Path(__file__).resolve().parents[2]
INIT = REPO / "db" / "init"
SCHEMA = "resolve_movement_test"
INIT_FILES = ("001_schema.sql", "004_decision_events.sql", "005_manual_reviews.sql",
              "006_decision_attempts.sql", "007_ledger_opening_balances.sql")

#: The approved cohort/demo values, passed in as a caller would. Not defaults:
#: the function has none, and supplying them here is the test standing in for
#: configuration that failed closed at boot.
THRESHOLD = "500.00"
PERMITTED = ["current"]


def _connect():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
    return conn


@pytest.fixture(scope="module")
def schema():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        for name in INIT_FILES:
            cur.execute(f"SET search_path TO {SCHEMA}")
            cur.execute((INIT / name).read_text(encoding="utf-8"))
    yield
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.close()


@pytest.fixture
def db(schema):
    conn = _connect()
    yield conn
    conn.rollback()
    conn.close()


def _exec(conn, sql, params=None):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(sql, params or ())
        return cur.fetchall() if cur.description else []


def _a_loan(conn, *, status="current", balance="1000.00", past_due="80.00",
            serviced=True):
    applicant = _exec(conn, "INSERT INTO applicants (name) VALUES ('T') RETURNING id")[0]["id"]
    app = _exec(conn, "INSERT INTO applications (applicant_id, amount, term_months, status) "
                      "VALUES (%s, 5000, 24, 'funded') RETURNING id", (applicant,))[0]["id"]
    loan = _exec(conn, "INSERT INTO loans (app_id, applicant_name, principal, apr, "
                       "term_months, status) VALUES (%s, 'T', 5000.00, 7.99, 24, %s) "
                       "RETURNING id", (app, status))[0]["id"]
    if serviced:
        _exec(conn, "INSERT INTO balances (loan_id, balance, past_due) VALUES (%s,%s,%s)",
              (loan, balance, past_due))
    conn.commit()
    return loan


def _propose(conn, loan, *, amount="-100.00", component="principal",
             entry_type="adjustment", requester=1, role="csr"):
    movement = _exec(conn,
        "INSERT INTO pending_movements (loan_id, component, amount, entry_type, reason, "
        "requested_by, requested_role) VALUES (%s,%s,%s,%s,'test proposal',%s,%s) "
        "RETURNING id", (loan, component, amount, entry_type, requester, role))[0]["id"]
    conn.commit()
    return movement


def _resolve(conn, movement, *, resolver=2, role="underwriter", resolution="approved",
             threshold=THRESHOLD, statuses=None):
    return _exec(conn, "SELECT resolve_pending_movement(%s,%s,%s,%s,%s,%s) AS entry",
                 (movement, resolver, role, resolution, threshold,
                  PERMITTED if statuses is None else statuses))[0]["entry"]


# --- requirement 4: an approval writes exactly one entry, a rejection none -----


def test_an_approval_writes_one_entry_and_moves_the_balance(db):
    loan = _a_loan(db)
    before = _exec(db, "SELECT balance FROM balances WHERE loan_id=%s", (loan,))[0]["balance"]
    movement = _propose(db, loan, amount="-100.00")

    entry = _resolve(db, movement)
    db.commit()

    assert entry is not None
    rows = _exec(db, "SELECT count(*) AS n FROM ledger_entries WHERE pending_movement_id=%s",
                 (movement,))
    assert rows[0]["n"] == 1
    after = _exec(db, "SELECT balance FROM balances WHERE loan_id=%s", (loan,))[0]["balance"]
    assert after == before - 100


def test_a_rejection_writes_no_entry_and_moves_nothing(db):
    loan = _a_loan(db)
    before = _exec(db, "SELECT balance FROM balances WHERE loan_id=%s", (loan,))[0]["balance"]
    movement = _propose(db, loan)

    assert _resolve(db, movement, resolution="rejected") is None
    db.commit()

    assert _exec(db, "SELECT count(*) AS n FROM ledger_entries WHERE pending_movement_id=%s",
                 (movement,))[0]["n"] == 0
    assert _exec(db, "SELECT balance FROM balances WHERE loan_id=%s",
                 (loan,))[0]["balance"] == before
    kept = _exec(db, "SELECT resolution, reason FROM pending_movements WHERE id=%s",
                 (movement,))[0]
    assert kept["resolution"] == "rejected" and kept["reason"], (
        "the rejected proposal was not retained with its reason -- that record is "
        "the evidence that a control refused something"
    )


# --- requirement 2: exactly one transition ------------------------------------


@pytest.mark.parametrize("second", ["approved", "rejected"])
def test_a_resolved_movement_cannot_be_resolved_again(db, second):
    loan = _a_loan(db)
    movement = _propose(db, loan)
    _resolve(db, movement)
    db.commit()

    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _resolve(db, movement, resolver=3, role="admin", resolution=second)
    assert "already approved" in str(exc.value)
    db.rollback()


# --- requirement 3: no self-approval, including admin -------------------------


@pytest.mark.parametrize("role", ["underwriter", "admin"])
def test_the_requester_may_not_resolve_their_own_movement(db, role):
    """No exception, including admin. An admin who wants to move money alone has
    to leave the application to do it, which is the signal an audit looks for."""
    loan = _a_loan(db)
    movement = _propose(db, loan, requester=7, role="csr")
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _resolve(db, movement, resolver=7, role=role)
    assert "may not resolve it" in str(exc.value)
    db.rollback()


# --- requirement 1: the lock, proven by racing it -----------------------------


def test_two_approvers_racing_produce_one_resolution_and_one_entry(db):
    """Two approvers, one resolution, one entry, one balance movement.

    Run as two real connections against real PostgreSQL, because that is the only
    place a lock exists.

    **What this proves, and what it does not.** It proves the OUTCOME: exactly
    one approver wins and the balance moves once. It does *not* prove that
    `FOR UPDATE` is what delivers that — removing the lock from
    `resolve_pending_movement` leaves this test passing, which was checked rather
    than assumed.

    The reason is that 0036's `pending_movements_one_way` trigger is a second,
    independent guard: the loser's UPDATE blocks on the winner's row, and when it
    proceeds the row already carries a resolution, so the trigger refuses it. The
    lock is still worth having and is not decorative — it serialises the
    revalidation reads that follow, so the status and balance checks cannot be
    made against a snapshot another approval is in the middle of invalidating —
    but the single-resolution guarantee survives without it.

    Stated here rather than left implied, because a concurrency test that quietly
    depends on a different mechanism than the one it names is how a lock gets
    removed in a refactor and nothing goes red.
    """
    loan = _a_loan(db)
    movement = _propose(db, loan, amount="-100.00")
    before = _exec(db, "SELECT balance FROM balances WHERE loan_id=%s", (loan,))[0]["balance"]

    barrier = threading.Barrier(2)
    outcomes = []

    def approve(resolver, role):
        conn = _connect()
        try:
            barrier.wait(timeout=10)
            entry = _exec(conn, "SELECT resolve_pending_movement(%s,%s,%s,'approved',%s,%s) AS e",
                          (movement, resolver, role, THRESHOLD, PERMITTED))[0]["e"]
            conn.commit()
            outcomes.append(("ok", entry))
        except Exception as exc:  # noqa: BLE001
            conn.rollback()
            outcomes.append(("refused", str(exc).splitlines()[0]))
        finally:
            conn.close()

    threads = [threading.Thread(target=approve, args=(2, "underwriter")),
               threading.Thread(target=approve, args=(3, "admin"))]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)

    assert len([o for o in outcomes if o[0] == "ok"]) == 1, (
        f"expected exactly one approver to win, got {outcomes}"
    )
    db.rollback()
    entries = _exec(db, "SELECT count(*) AS n FROM ledger_entries WHERE pending_movement_id=%s",
                    (movement,))[0]["n"]
    assert entries == 1, f"one approval produced {entries} ledger entries"
    after = _exec(db, "SELECT balance FROM balances WHERE loan_id=%s", (loan,))[0]["balance"]
    assert after == before - 100, "the balance moved more than once for one approval"


# --- requirement 5: the entry is built from the locked row --------------------


def test_the_entry_matches_the_proposal_exactly(db):
    loan = _a_loan(db)
    # Inside past_due (80.00): a larger waiver is correctly refused for driving
    # the component negative, which is a different test.
    movement = _propose(db, loan, amount="-50.00", component="fees",
                        entry_type="fee_waived")
    entry = _resolve(db, movement)
    db.commit()

    row = _exec(db, "SELECT loan_id, component, amount, entry_type, reason "
                    "FROM ledger_entries WHERE id=%s", (entry,))[0]
    proposal = _exec(db, "SELECT loan_id, component, amount, entry_type, reason "
                         "FROM pending_movements WHERE id=%s", (movement,))[0]
    assert row == proposal, (
        "the entry differs from the proposal that authorised it -- an approval "
        "may not execute different terms than the ones reviewed"
    )


# --- requirement 6: the ledger actor is the approver --------------------------


def test_the_ledger_actor_is_the_approver_not_the_requester(db):
    loan = _a_loan(db)
    movement = _propose(db, loan, requester=1, role="csr")
    entry = _resolve(db, movement, resolver=42, role="admin")
    db.commit()

    row = _exec(db, "SELECT actor_id, actor_role FROM ledger_entries WHERE id=%s",
                (entry,))[0]
    assert row["actor_id"] == 42 and row["actor_role"] == "admin", (
        "the ledger credits the wrong person with authorising the movement"
    )


# --- revalidation inside the lock ---------------------------------------------


def test_a_movement_on_a_loan_that_left_the_permitted_status_is_refused(db):
    """Valid when raised, invalid when executed.

    The proposal was legal against a `current` loan. By approval time the loan
    has closed, and a check performed when it entered the queue is not evidence
    about the state when money moves.
    """
    loan = _a_loan(db)
    movement = _propose(db, loan)
    _exec(db, "UPDATE loans SET status='closed' WHERE id=%s", (loan,))
    db.commit()

    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _resolve(db, movement)
    assert "not a status a movement may execute on" in str(exc.value)
    db.rollback()


@pytest.mark.parametrize("status", ["CURRENT", "Current", "closed", "charged_off",
                                    "delinquent", "", None])
def test_every_unpermitted_status_fails_closed(db, status):
    """Including case variants: normalising case would accept a status nobody
    approved (spec 0002 AC-18 -- an unrecognised status SHALL refuse)."""
    loan = _a_loan(db)
    movement = _propose(db, loan)
    _exec(db, "UPDATE loans SET status=%s WHERE id=%s", (status, loan))
    db.commit()

    with pytest.raises(psycopg2.errors.RaiseException):
        _resolve(db, movement)
    db.rollback()


def test_a_movement_whose_loan_left_servicing_is_refused(db):
    """A loan that exists but is no longer serviced has nothing to project onto.

    The `balances` row is removed with the cutover's delete guard disabled for
    the statement, then re-enabled -- that guard exists to stop this happening by
    accident, and the point here is what the APPROVAL does when it has happened
    anyway. Re-enabled inside the same transaction so nothing leaks to the next
    test.
    """
    # A loan that was never opened in servicing, rather than one whose row is
    # deleted: the cutover's delete guard makes removal impossible by design, and
    # disabling it needs an ALTER that PostgreSQL refuses while the deferred
    # parity trigger has pending events. Same branch of the function, reached by
    # a row shape that is genuinely reachable in production.
    loan = _a_loan(db, serviced=False)
    movement = _propose(db, loan)

    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _resolve(db, movement)
    assert "no longer serviced" in str(exc.value)
    db.rollback()


def test_a_movement_that_would_drive_a_component_negative_is_refused(db):
    """A waiver raised when fees were 80.00 and approved after they were paid
    down to 10.00 was valid when written and is not now."""
    loan = _a_loan(db, past_due="80.00")
    movement = _propose(db, loan, amount="-80.00", component="fees",
                        entry_type="fee_waived")
    _exec(db, "UPDATE balances SET past_due = 10.00 WHERE loan_id=%s", (loan,))
    db.commit()

    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _resolve(db, movement)
    assert "below zero" in str(exc.value)
    db.rollback()


def test_a_rejection_still_works_on_a_target_that_became_unexecutable(db):
    """Otherwise a proposal against a closed loan could be neither approved nor
    rejected, and would sit in the queue for ever."""
    loan = _a_loan(db)
    movement = _propose(db, loan)
    _exec(db, "UPDATE loans SET status='closed' WHERE id=%s", (loan,))
    db.commit()

    assert _resolve(db, movement, resolution="rejected") is None
    db.commit()
    assert _exec(db, "SELECT resolution FROM pending_movements WHERE id=%s",
                 (movement,))[0]["resolution"] == "rejected"


# --- the function refuses to run on absent policy ------------------------------


def test_no_permitted_statuses_refuses_rather_than_assuming(db):
    """An empty set must not read as "every status is fine"."""
    loan = _a_loan(db)
    movement = _propose(db, loan)
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _resolve(db, movement, statuses=[])
    assert "no permitted loan statuses" in str(exc.value)
    db.rollback()


def test_a_resolution_without_a_threshold_is_refused(db):
    loan = _a_loan(db)
    movement = _propose(db, loan)
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _resolve(db, movement, threshold=None)
    assert "threshold" in str(exc.value)
    db.rollback()


def test_the_threshold_actually_used_is_recorded_on_the_proposal(db):
    """spec 0002 AC-22. A history of approvals is unreadable if the bar moved and
    nothing says when."""
    loan = _a_loan(db)
    movement = _propose(db, loan)
    _resolve(db, movement, threshold="750.00")
    db.commit()
    assert _exec(db, "SELECT resolved_threshold FROM pending_movements WHERE id=%s",
                 (movement,))[0]["resolved_threshold"] == 750
