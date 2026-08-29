"""A capped page must say it is capped, and paging must not lose anyone.

`snapshot()` reads at most 50 pending and 25 resolved proposals. That bound was
invisible: the response carried two arrays and nothing else, and the approvals
page rendered them under headings that read as the whole queue. Past the cap a
real request, raised by real staff and waiting on a real approver, was simply
not on the screen -- with nothing anywhere saying more existed.

That is a DIFFERENT defect from MC-RACE-01, and worth keeping apart from it.
MC-RACE-01 is a torn read: two statements around one commit, and a movement in
neither half or in both. This is truncation: one statement, correctly
partitioned, showing a prefix of the answer as though it were all of it. The
first is fixed by reading once; the second is not fixed by reading once at all.

The unit tests beside this file answer from a fake, so they can prove the
totals are REQUESTED and returned. They cannot prove the totals are TRUE, that
a page boundary does not drop or repeat a row, or that the bounds survive an
offset past the end -- those are properties of the statement against real data.
This file proves them against real Postgres.

It also re-proves the property MC-RACE-01 cost the most to establish: adding
counts to the statement must not have split it. A count answered by a second
query would be read at a different instant from the items -- 63 pending beside
50 rows read before the 63rd arrived -- which is the same torn read moved from
the rows to the total. So the statement count is asserted here too, against real
data rather than only against a fake.

**Isolation.** Same containment as
`test_movement_snapshot_is_consistent_under_concurrency.py`, and for the same
reason: this file needs more than 50 pending proposals, and creating those in
the shared schema would drop 63 pieces of fabricated staff work into the real
approvals queue. The real DDL is loaded into a dedicated schema, production
constraints and triggers stay live inside it, individual rows are never deleted
(the retention trigger refuses, correctly), and the whole schema is dropped when
the module finishes.
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

SCHEMA = "servicing_bounds_test"
SCHEMA_SQL = (
    pathlib.Path(__file__).resolve().parents[3] / "db" / "init" / "001_schema.sql"
)

THRESHOLD = "500.00"
PERMITTED = ["current"]

RAISER_ID, RESOLVER_ID = 1, 2
LOAN_ID = 1

#: Deliberately past both caps: 63 pending against a limit of 50, 30 resolved
#: against a limit of 25. A fixture that stopped at the cap would make every
#: assertion below vacuous while still looking like a pagination test.
PENDING_COUNT = 63
RESOLVED_COUNT = 30


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


def _raise(conn, note: str) -> int:
    row = _one(
        conn,
        "INSERT INTO pending_movements "
        "  (loan_id, component, amount, entry_type, reason, requested_by, requested_role) "
        "VALUES (%s, 'fees', %s, 'adjustment', %s, %s, 'underwriter') RETURNING id",
        (LOAN_ID, "5.00",
         f"automated approvals-bounds check ({note}); not a real request", RAISER_ID),
    )
    return int(row["id"])


def _reject(conn, movement_id: int) -> None:
    """The pending -> resolved transition, through the function that owns it.

    A REJECTION throughout, as in the concurrency proof: it uses the same
    one-way transition an approval uses and writes no ledger entry, so building
    30 resolved proposals here moves no money.
    """
    _one(
        conn,
        "SELECT resolve_pending_movement(%s, %s, %s, %s, %s, %s) AS entry_id",
        (movement_id, RESOLVER_ID, "admin", "rejected", THRESHOLD, PERMITTED),
    )


@pytest.fixture(scope="module")
def populated():
    """A schema holding more proposals than either cap admits."""
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(SCHEMA_SQL.read_text(encoding="utf-8"))
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

    worker = _connect()
    resolved_ids = [_raise(worker, f"resolved {i}") for i in range(RESOLVED_COUNT)]
    for movement_id in resolved_ids:
        _reject(worker, movement_id)
    # Raised AFTER the resolved ones so that "oldest first" and "highest id
    # first" are different orders. A fixture that made them agree could not
    # tell a correct page boundary from an id-ordered one.
    pending_ids = [_raise(worker, f"pending {i}") for i in range(PENDING_COUNT)]
    worker.close()

    yield {"pending": pending_ids, "resolved": resolved_ids}

    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.close()


@pytest.fixture
def reader(populated):
    """Points servicing's own shared connection at this schema, and back."""
    shared = maker_checker.db.get_conn()
    with shared.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
    yield populated
    with shared.cursor() as cur:
        cur.execute("SET search_path TO public")


def test_the_fixture_really_does_exceed_both_caps(reader):
    """Otherwise every assertion below is vacuously true."""
    assert PENDING_COUNT > 50 and RESOLVED_COUNT > 25
    body = maker_checker.snapshot()
    assert len(body["movements"]) == 50, "the pending half is not actually capped"
    assert len(body["resolved"]) == 25, "the resolved half is not actually capped"


def test_the_totals_are_true_and_not_the_page_size(reader):
    """The defect, stated as an assertion: 50 shown, 63 waiting, and the
    response says so."""
    body = maker_checker.snapshot()

    assert body["bounds"]["pending"] == {
        "total": PENDING_COUNT, "limit": 50, "offset": 0,
    }
    assert body["bounds"]["resolved"] == {
        "total": RESOLVED_COUNT, "limit": 25, "offset": 0,
    }


def test_no_pending_proposal_is_invisible_merely_because_of_the_cap(reader):
    """Every raised proposal is reachable by paging -- none is lost to the cap.

    This is the borrower-facing shape of the defect: a request that exists, is
    waiting on a human, and cannot be got to from the screen that exists to show
    it.
    """
    seen: list[int] = []
    offset = 0
    while True:
        body = maker_checker.snapshot(pending_offset=offset)
        seen.extend(m["id"] for m in body["movements"])
        offset += body["bounds"]["pending"]["limit"]
        if offset >= body["bounds"]["pending"]["total"]:
            break

    assert sorted(seen) == sorted(reader["pending"])


def test_paging_the_pending_half_neither_repeats_nor_drops_a_row(reader):
    """A non-unique sort key under LIMIT/OFFSET lets rows repeat on one page and
    vanish from the next -- the defect `routers/loans.py` documents for
    `opened_at`. `requested_at` has the same exposure here: these proposals are
    created in the same transaction-free loop and can share a timestamp, so the
    order is `(requested_at, id)` and `id` is what breaks the tie."""
    first = maker_checker.snapshot(pending_limit=20, pending_offset=0)["movements"]
    second = maker_checker.snapshot(pending_limit=20, pending_offset=20)["movements"]
    third = maker_checker.snapshot(pending_limit=20, pending_offset=40)["movements"]

    ids = [m["id"] for m in first + second + third]
    assert len(ids) == len(set(ids)), "a proposal appeared on two pages"
    assert set(ids) <= set(reader["pending"])
    assert len(ids) == 60


def test_paging_is_deterministic_across_identical_requests(reader):
    """The same page twice is the same page. Without a total order it need not
    be, and the failure is silent -- a row simply moves between pages."""
    once = [m["id"] for m in
            maker_checker.snapshot(pending_limit=20, pending_offset=20)["movements"]]
    twice = [m["id"] for m in
             maker_checker.snapshot(pending_limit=20, pending_offset=20)["movements"]]

    assert once == twice


def test_the_resolved_half_pages_on_its_own(reader):
    """Two panels, two offsets. One shared offset would page the resolved
    history every time an approver stepped through the pending queue."""
    body = maker_checker.snapshot(pending_offset=50, resolved_offset=0)

    assert len(body["movements"]) == PENDING_COUNT - 50
    assert len(body["resolved"]) == 25
    assert body["bounds"]["pending"]["offset"] == 50
    assert body["bounds"]["resolved"]["offset"] == 0


def test_an_offset_past_the_end_still_reports_the_true_total(reader):
    """The empty-page case, which is where a count computed from the returned
    rows would quietly lie.

    An aggregate with no GROUP BY returns one row even when it counts nothing,
    which is why the statement LEFT JOINs from the counts to the items rather
    than the other way round. The honest answer here is "63 pending, and you are
    past them" -- not "none, and none exist".
    """
    body = maker_checker.snapshot(pending_offset=1000, resolved_offset=1000)

    assert body["movements"] == []
    assert body["resolved"] == []
    assert body["bounds"]["pending"]["total"] == PENDING_COUNT
    assert body["bounds"]["resolved"]["total"] == RESOLVED_COUNT


def test_the_totals_did_not_cost_a_second_statement(reader):
    """The regression that would undo MC-RACE-01 while looking like a feature.

    Counting in a separate query answers at a different instant from the items,
    which is the same torn read `snapshot()` exists to prevent -- moved from the
    rows to the total. Asserted here against the real database, not only against
    the fake beside it.
    """
    original = maker_checker.db.query
    count = {"n": 0}

    def _counting(sql, params=None):
        count["n"] += 1
        return original(sql, params)

    maker_checker.db.query = _counting
    try:
        maker_checker.snapshot()
    finally:
        maker_checker.db.query = original

    assert count["n"] == 1, (
        f"{count['n']} reads -- the totals are being counted separately from the "
        "items, so they describe a different instant"
    )


def test_a_resolution_between_pages_cannot_put_a_movement_in_both_halves(reader):
    """Paging did not reintroduce the race within a single page load.

    Each response is still one statement, so the two halves of ONE response
    still partition. This fires a real commit from a second session at the seam
    -- after the statement returns -- and asserts the response that was already
    taken is internally consistent.
    """
    movement_id = reader["pending"][0]

    body = maker_checker.snapshot(pending_limit=500, resolved_limit=500)
    in_pending = any(m["id"] == movement_id for m in body["movements"])
    in_resolved = any(m["id"] == movement_id for m in body["resolved"])
    assert in_pending != in_resolved

    other = _connect()
    try:
        _reject(other, movement_id)
    finally:
        other.close()

    after = maker_checker.snapshot(pending_limit=500, resolved_limit=500)
    assert not any(m["id"] == movement_id for m in after["movements"])
    assert any(m["id"] == movement_id for m in after["resolved"])
    # And the totals moved with it, rather than being cached from the earlier read.
    assert after["bounds"]["pending"]["total"] == PENDING_COUNT - 1
    assert after["bounds"]["resolved"]["total"] == RESOLVED_COUNT + 1
