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

The file runs in this order, deliberately:

  1. the two-read shape loses a movement -- resolved read, commit, pending read,
     and the movement is in NEITHER half;
  2. the two-read shape double-counts one -- the opposite interleaving, pending
     read, commit, resolved read, and the movement is in BOTH halves.
     Together these are the defect, reproduced in both directions, so what
     follows is not a test of a race that could never happen;
  3. `snapshot()` holds the movement in one half before the resolution;
  4. `snapshot()` holds it in the other half after the resolution;
  5. `snapshot()` cannot be straddled by a commit fired at the seam -- the
     property, and the regression this file exists to catch;
  6. `snapshot()` issues exactly one read, said plainly against real Postgres.

**How the interleaving is forced.** *Not* by timing. An earlier version of this
file raced a resolver thread against a `threading.Barrier` and hoped its commit
would land mid-read; with `snapshot()` mutated back to two separate reads -- the
exact regression it existed to catch -- **it still passed**, because both reads
finished before the other session committed. That approach is discarded, and
nothing here sleeps, races or repeats a run hoping for a different interleaving.

What replaced it is the SEAM. Two reads have an instant between them at which
another transaction can commit; one statement has no such instant. So case 5
fires the resolution exactly there -- from a second connection, in the gap after
a query returns and before control comes back -- by wrapping `db.query`. That is
deterministic: it targets the property rather than the scheduler, and it fails
on the defect, which is the only thing that made it worth writing.

**The transition is a REJECTION, on purpose.** Rejecting moves a proposal from
pending to resolved through the same one-way transition an approval uses, and
writes no ledger entry -- so this file can run repeatedly without moving money.
What is under test is the READ.

**Nothing this file writes reaches the application schema.** It loads the real
DDL into a dedicated schema of its own and drops that schema when the module
finishes. Individual proposal rows are NOT deleted -- the database refuses
(`pending_movements_are_retained()`), and that refusal is correct and not this
file's to route around -- but the rows go when the schema does, so no fabricated
approval work survives into `public`. See the `schema` and `raised` fixtures.
"""
import os
import pathlib

import psycopg2
import psycopg2.extras
import pytest

from app import maker_checker

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against"
)

#: This file builds its OWN schema and does not touch whatever else is in the
#: database. CI sets DATABASE_URL for the servicing job but never loads the
#: application schema into it, so the first version -- which read the seeded
#: `users` and `loans` -- passed locally against a compose stack and failed in
#: CI with `relation "users" does not exist`. A DATABASE_URL is a database, not
#: a seeded one, and the guard above cannot tell the difference.
SCHEMA = "servicing_snapshot_test"

#: The real DDL, loaded verbatim. `resolve_pending_movement` is the transition
#: under test and it depends on the ledger tables, the projection trigger, the
#: retention trigger and the `no_self_approval` / `resolution_complete`
#: constraints. Hand-copying that subset would be a second, quietly diverging
#: copy of the schema -- the failure mode this repository keeps correcting --
#: so the file itself is loaded into a dedicated schema instead. It carries no
#: `CREATE SCHEMA`, no `public.` qualification and no extensions, which is what
#: makes that possible.
SCHEMA_SQL = (
    pathlib.Path(__file__).resolve().parents[3] / "db" / "init" / "001_schema.sql"
)

#: The bar a resolution is judged against. Passed explicitly; this file states no
#: figure of its own beyond what it needs to drive the function.
THRESHOLD = "500.00"
PERMITTED = ["current"]

RAISER_ID, RESOLVER_ID = 1, 2
LOAN_ID = 1


def _connect():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
    return conn


def _one(conn, sql, params=None):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or ())
        return cur.fetchone() if cur.description else None


@pytest.fixture(scope="module")
def schema():
    """A dedicated schema holding the real objects, dropped afterwards."""
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
        # A raiser and a DIFFERENT resolver: `no_self_approval` applies here as
        # it does anywhere else, and a fixture that ignored it would be
        # modelling a database that cannot exist.
        cur.execute(
            "INSERT INTO users (id, username, password_hash, role) VALUES "
            "(%s, 'underwriter-test', 'x', 'underwriter'), "
            "(%s, 'admin-test', 'x', 'admin')",
            (RAISER_ID, RESOLVER_ID),
        )
        cur.execute(
            "INSERT INTO loans (id, principal, note_rate_pct, term_months, status) "
            "VALUES (%s, 15000.00, 7.99, 36, 'current')", (LOAN_ID,),
        )
        cur.execute(
            "INSERT INTO balances (loan_id, balance, past_due) VALUES (%s, 15000.00, 0)",
            (LOAN_ID,),
        )
    yield conn
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.close()


@pytest.fixture
def conn(schema):
    c = _connect()
    yield c
    c.close()


@pytest.fixture
def reader(schema, monkeypatch):
    """Points servicing's own shared connection at this schema.

    `snapshot()` reads through `maker_checker.db`, so the read under test has to
    resolve names here rather than wherever the shared connection was pointing.
    Restored afterwards so no other test in the session inherits it.
    """
    shared = maker_checker.db.get_conn()
    with shared.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
    yield
    with shared.cursor() as cur:
        cur.execute("SET search_path TO public")


@pytest.fixture
def raised(conn, reader):
    """Proposals created by this file. They are NOT deleted, by instruction.

    The first version deleted them in teardown. The database refused:

        pending movement 12 may not be deleted: proposals are retained as the
        evidence of what staff asked for (rejected)

    -- `pending_movements_are_retained()`. That is the right answer and the
    trigger is not this file's to route around: a record of what somebody asked
    for is the point of the table. The rows go when the schema does.
    """
    def _raise(note: str) -> int:
        row = _one(
            conn,
            "INSERT INTO pending_movements "
            "  (loan_id, component, amount, entry_type, reason, requested_by, requested_role) "
            "VALUES (%s, 'fees', %s, 'adjustment', %s, %s, 'underwriter') RETURNING id",
            (LOAN_ID, "5.00",
             f"automated snapshot-consistency check ({note}); not a real request", RAISER_ID),
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


def _two_reads_resolved_first(
    movement_id: int, resolver_conn, resolver: int
) -> tuple[bool, bool]:
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


def _two_reads_pending_first(
    movement_id: int, resolver_conn, resolver: int
) -> tuple[bool, bool]:
    """The OLD shape, the other way round: pending read, a commit, resolved read.

    Same two reads, opposite order. The commit lands between them either way;
    which half of the page is read first decides whether the movement is lost or
    shown twice.
    """
    pending_first = any(m["id"] == movement_id for m in maker_checker.queue(limit=500))
    _reject(resolver_conn, movement_id, resolver)
    resolved_after = any(
        m["id"] == movement_id for m in maker_checker.resolved(limit=500)
    )
    return pending_first, resolved_after


def test_the_two_read_shape_really_does_lose_a_movement(conn, reader, raised):
    """The defect, reproduced. Without this the cases below prove nothing about
    a race that might never have been possible."""
    movement_id = raised("two-read shape, resolved first")

    in_pending, in_resolved = _two_reads_resolved_first(movement_id, conn, RESOLVER_ID)

    assert (in_pending, in_resolved) == (False, False), (
        "expected the movement to fall between two separate reads -- if it did "
        "not, this interleaving no longer reproduces MC-RACE-01 and the rest of "
        "this file is testing a race that cannot happen"
    )


def test_the_two_read_shape_really_does_show_a_movement_twice(conn, reader, raised):
    """The same defect from the other side.

    MC-RACE-01 names two outcomes, and only one of them is a movement going
    missing. Read the halves in the opposite order around the same commit and
    the movement is in BOTH -- pending when the first read ran, resolved when the
    second did. An approver would be shown one request as two, one of them
    already decided and still offering buttons.

    This is reproduction, not the regression proof: the seam test below is what
    fails if `snapshot()` ever goes back to two reads. It is here because a file
    that demonstrates only the NEITHER ordering leaves the BOTH ordering
    asserted nowhere but in a commit message.
    """
    movement_id = raised("two-read shape, pending first")

    in_pending, in_resolved = _two_reads_pending_first(movement_id, conn, RESOLVER_ID)

    assert (in_pending, in_resolved) == (True, True), (
        "expected the movement in BOTH halves across two separate reads -- if it "
        "was not, this interleaving no longer reproduces the duplicate half of "
        "MC-RACE-01"
    )


def test_one_snapshot_holds_the_movement_before_the_resolution(conn, reader, raised):
    movement_id = raised("before")

    assert _halves(movement_id) == (True, False)


def test_one_snapshot_holds_the_movement_after_the_resolution(conn, reader, raised):
    movement_id = raised("after")
    _reject(conn, movement_id, RESOLVER_ID)

    assert _halves(movement_id) == (False, True)


def test_a_commit_cannot_land_between_the_halves_of_one_snapshot(conn, reader, raised):
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
                _reject(other, movement_id, RESOLVER_ID)
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


def test_the_snapshot_issues_exactly_one_read(conn, reader, raised):
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
