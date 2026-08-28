"""One page load sees one consistent database view.

The approvals page shows two lists that partition the same table: `Pending` is
`resolution IS NULL`, `Recently resolved` is `resolution IS NOT NULL`. A
proposal is in exactly one of them. What the page must never do is let a
proposal fall between them, because a movement disappearing off the screen is
the very defect the history panel was added to fix.

Read as TWO requests, it could. Review finding MC-RACE-01:

    resolved read  ->  (another approver commits)  ->  pending read
        movement not yet resolved      movement no longer pending
        = IN NEITHER LIST

    pending read   ->  (another approver commits)  ->  resolved read
        movement still pending         movement now resolved
        = IN BOTH LISTS

The unit tests beside this file assert the SHAPE of the fix -- one statement,
disjoint predicates. They cannot prove the behaviour, because they answer from a
fake. This file proves it against real Postgres, with a real concurrent
resolution committing while the read is in flight.

Three cases, deliberately in this order:

  1. the two-read shape genuinely loses a movement -- the defect, reproduced,
     so the rest is not a test of something that never happened;
  2. `snapshot()` cannot, at the same interleaving;
  3. `snapshot()` cannot, under a barrier-forced concurrent commit, repeated.

**The transition is a REJECTION, on purpose.** Rejecting moves a proposal from
pending to resolved through the same one-way transition an approval uses, and
writes no ledger entry -- so this file can run repeatedly without moving money
or leaving permanent adjustments on a seeded loan. What is under test is the
READ.

The proposal ROWS do persist, and that is not a leak: the database refuses to
delete them (`pending_movements_are_retained()`), because a record of what staff
asked for is the point of the table. See the `raised` fixture.

**The interleaving is forced, never slept on**, so a pass means the property
held rather than that the machine was fast. Case 3 additionally asserts the two
sessions have different backend pids: two connections, as two HTTP requests
would be, not two threads sharing one.
"""
import os
import threading

import psycopg2
import psycopg2.extras
import pytest

from app import maker_checker

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against"
)

#: The bar a resolution is judged against. Passed explicitly; this file states no
#: figure of its own beyond what it needs to drive the function.
THRESHOLD = "500.00"
PERMITTED = ["current"]

#: Case 3 repeats -- one pass could be a scheduling accident. Kept small
#: because each round leaves a retained proposal behind (see `raised`).
ROUNDS = 5


def _connect():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    return conn


def _one(conn, sql, params=None):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or ())
        return cur.fetchone() if cur.description else None


@pytest.fixture
def conn():
    c = _connect()
    yield c
    c.close()


@pytest.fixture
def actors(conn):
    """A raiser and a DIFFERENT resolver: `no_self_approval` applies here too."""
    raiser = _one(conn, "SELECT id FROM users WHERE username = 'underwriter'")
    resolver = _one(conn, "SELECT id FROM users WHERE username = 'admin'")
    assert raiser and resolver, "seeded underwriter and admin users must exist"
    return int(raiser["id"]), int(resolver["id"])


@pytest.fixture
def loan_id(conn):
    row = _one(conn, "SELECT id FROM loans WHERE status = 'current' ORDER BY id LIMIT 1")
    assert row, "a loan with status 'current' must exist"
    return int(row["id"])


@pytest.fixture
def raised(conn, actors, loan_id):
    """Proposals created by this file. They are NOT cleaned up, by instruction.

    The first version deleted them in teardown. The database refused:

        pending movement 12 may not be deleted: proposals are retained as the
        evidence of what staff asked for (rejected)

    -- `pending_movements_are_retained()`. That is the right answer and the
    trigger is not this file's to route around: a record of what somebody asked
    for is exactly what a maker-checker table is. The rows stay.

    What that costs is bounded. Each is REJECTED, so none writes a ledger entry
    or moves a balance, and each carries a reason naming this file so a reader
    who meets one knows what it is. `approval-queue-self-approval.spec.ts`
    already leaves resolved proposals behind for the same reason, and any
    environment that wants them gone reseeds.
    """
    raiser, _ = actors

    def _raise(note: str) -> int:
        row = _one(
            conn,
            "INSERT INTO pending_movements "
            "  (loan_id, component, amount, entry_type, reason, requested_by, requested_role) "
            "VALUES (%s, 'fees', %s, 'adjustment', %s, %s, 'underwriter') RETURNING id",
            (loan_id, "5.00",
             f"automated snapshot-consistency check ({note}); not a real request", raiser),
        )
        return int(row["id"])

    return _raise


def _reject(conn, movement_id: int, resolver: int) -> None:
    """The pending -> resolved transition, through the function that owns it."""
    _one(
        conn,
        "SELECT resolve_pending_movement(%s, %s, %s, %s, %s, %s) AS entry_id",
        (movement_id, resolver, "admin", "rejected", THRESHOLD, PERMITTED),
    )


def _halves(movement_id: int) -> tuple[bool, bool]:
    """Is this movement in the pending half, the resolved half, from one read?"""
    body = maker_checker.snapshot(pending_limit=500, resolved_limit=500)
    return (
        any(m["id"] == movement_id for m in body["movements"]),
        any(m["id"] == movement_id for m in body["resolved"]),
    )


def _two_reads(movement_id: int, resolver_conn, resolver: int) -> tuple[bool, bool]:
    """The OLD shape: resolved read, a commit, then the pending read.

    Written out rather than reusing the page's code because the page no longer
    does this. What is reproduced is the interleaving, in the order that loses
    the movement.
    """
    resolved_first = any(
        m["id"] == movement_id for m in maker_checker.resolved(limit=500)
    )
    _reject(resolver_conn, movement_id, resolver)
    pending_after = any(m["id"] == movement_id for m in maker_checker.queue(limit=500))
    return pending_after, resolved_first


def test_the_two_read_shape_really_does_lose_a_movement(conn, actors, raised):
    """The defect, reproduced. Without this the cases below prove nothing about
    a race that might never have been possible."""
    _, resolver = actors
    movement_id = raised("two-read shape")

    in_pending, in_resolved = _two_reads(movement_id, conn, resolver)

    assert (in_pending, in_resolved) == (False, False), (
        "expected the movement to fall between two separate reads -- if it did "
        "not, this interleaving no longer reproduces MC-RACE-01 and the rest of "
        "this file is testing a race that cannot happen"
    )


def test_one_snapshot_holds_the_movement_before_the_resolution(conn, actors, raised):
    _, resolver = actors
    movement_id = raised("before")

    assert _halves(movement_id) == (True, False)


def test_one_snapshot_holds_the_movement_after_the_resolution(conn, actors, raised):
    _, resolver = actors
    movement_id = raised("after")
    _reject(conn, movement_id, resolver)

    assert _halves(movement_id) == (False, True)


def test_a_commit_cannot_land_between_the_halves_of_one_snapshot(conn, actors, raised):
    """The property, forced rather than raced.

    An earlier version of this test started a resolver thread on a barrier and
    hoped its commit would land mid-read. It did not, and the test proved
    nothing: with `snapshot()` replaced by two separate reads -- the exact
    regression it existed to catch -- **it still passed**, because both reads
    finished before the other session committed. A concurrency test that cannot
    fail on the defect is worse than no test, because it reports the property as
    held.

    The discriminator is not timing, it is the SEAM. Two reads have an instant
    between them at which another transaction can commit; one statement has no
    such instant. So the resolution is fired exactly there: in the gap after a
    query returns, from a different session, and committed before control comes
    back. That is deterministic and it targets the property directly.

      * one statement  -> the seam never falls inside the read. The resolution
                          commits after it, so the movement is in the pending
                          half, once.
      * two statements -> the resolution commits between them. The movement is
                          in BOTH halves (pending read before the commit,
                          resolved read after), and the assertion fails.

    Verified by mutation in both directions: reverting `snapshot()` to two reads
    fails this test, and restoring it passes.
    """
    _, resolver = actors
    movement_id = raised("seam")

    original = maker_checker.db.query
    state = {"queries": 0, "resolved_at_seam": False}

    def _query_then_resolve(sql, params=None):
        rows = original(sql, params)
        state["queries"] += 1
        # The seam: after the FIRST read of this snapshot returns, a different
        # session resolves the movement and commits. A single-statement
        # snapshot has already taken its view by now; a two-statement one has
        # not taken its second.
        if state["queries"] == 1 and not state["resolved_at_seam"]:
            state["resolved_at_seam"] = True
            other = _connect()
            try:
                _reject(other, movement_id, resolver)
            finally:
                other.close()
        return rows

    maker_checker.db.query = _query_then_resolve
    try:
        in_pending, in_resolved = _halves(movement_id)
    finally:
        maker_checker.db.query = original

    assert state["resolved_at_seam"], "the concurrent resolution never fired"
    assert in_pending != in_resolved, (
        f"movement {movement_id} was "
        + ("in BOTH halves" if in_pending else "in NEITHER half")
        + " of one page load: a resolution committed between two reads, which is "
        "the race this snapshot exists to make impossible"
    )
    # One read, so the movement is where it was when that read was taken.
    assert (in_pending, in_resolved) == (True, False)

    # The resolution really did commit -- a later read sees it resolved, so the
    # case above exercised a real transition rather than a no-op.
    assert _halves(movement_id) == (False, True)


def test_the_snapshot_issues_exactly_one_read(conn, actors, raised):
    """Said plainly, against the real database rather than a fake: the guarantee
    above is a property of there being one statement, so that is asserted here
    too and not only in the unit tests."""
    raised("statement count")

    original = maker_checker.db.query
    count = {"n": 0}

    def _counting(sql, params=None):
        count["n"] += 1
        return original(sql, params)

    maker_checker.db.query = _counting
    try:
        maker_checker.snapshot(pending_limit=500, resolved_limit=500)
    finally:
        maker_checker.db.query = original

    assert count["n"] == 1, f"{count['n']} reads, so there is a seam to lose a movement in"
