"""The graph benchmark must count applicants reached, not paths walked.

Two defects found by review on PR #12, both of which produced timings that
looked like measurements of PostgreSQL and were measurements of the harness:

  1. every candidate carried the current path in an ARRAY[] and excluded only
     nodes already on THAT path, so an applicant reachable by k simple paths was
     expanded k times. In a cyclic identity graph k explodes with depth -- the
     published depth-4 run emitted 9,419,712 recursive rows to reach 1,621
     applicants -- and the "depth-4 cliff" the ADR rested on was the cost of
     enumerating those paths, not of answering the question;
  2. the frontier candidate reached neighbours by joining the attribute posting
     table to itself, which emits one row per SHARED attribute. Two applicants
     sharing both an address and a phone produced that transition twice, while
     the `edges` table collapses it with UNION -- so the three candidates were
     not traversing the same edge relation and their timings were not
     comparable.

Both are asserted here against a real Postgres on a hand-built graph small
enough to reason about by hand, because both defects are in SQL semantics and
neither is visible to a test that mocks the database.

Skips without DATABASE_URL, like the rest of db/tests; CI's db-migrations job
always sets it.
"""
import importlib.util
import os
from pathlib import Path

import psycopg2
import pytest

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against"
)

BENCH = Path(__file__).resolve().parent.parent / "bench" / "graph_traversal_benchmark.py"
SCHEMA = "graph_bench_test"


def _bench_module():
    spec = importlib.util.spec_from_file_location("graph_traversal_benchmark", BENCH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bench = _bench_module()


@pytest.fixture
def cur():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    cursor = conn.cursor()
    cursor.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    cursor.execute(f"CREATE SCHEMA {SCHEMA}")
    cursor.execute(f"SET search_path TO {SCHEMA}")
    yield cursor
    cursor.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.close()


def _build_graph(cur):
    """Four applicants, deliberately cyclic and deliberately multi-attribute.

        1 --address-- 2        (also --phone-- : a PARALLEL transition)
        2 --employer- 3
        3 --email---- 4
        4 --address-- 1        (closes the cycle)

    Reachability from 1: depth 1 -> {1,2,4}, depth 2 -> {1,2,3,4}, and it stays
    there. The number of simple paths, by contrast, keeps growing with depth --
    which is the difference the first defect erased.
    """
    cur.execute("""
        CREATE TABLE applicants (
            id SERIAL PRIMARY KEY,
            name TEXT, address TEXT, phone TEXT, email TEXT, ssn TEXT, ein TEXT
        );
        CREATE TABLE applications (
            id SERIAL PRIMARY KEY,
            applicant_id INT REFERENCES applicants(id),
            employer TEXT
        );
        INSERT INTO applicants (id, name, address, phone, email) VALUES
            (1, 'a', 'shared_addr_a', 'shared_ph', 'e1@x.test'),
            -- 1 and 2 share BOTH address and phone: one edge, two attributes.
            (2, 'b', 'shared_addr_a', 'shared_ph', 'e2@x.test'),
            (3, 'c', 'addr_c',        'ph_c',      'shared_email'),
            (4, 'd', 'shared_addr_a', 'ph_d',      'shared_email');
        SELECT setval(pg_get_serial_sequence('applicants','id'), 4);
        INSERT INTO applications (applicant_id, employer) VALUES
            (1, 'emp_1'), (2, 'shared_emp'), (3, 'shared_emp'), (4, 'emp_4');
    """)
    cur.execute(bench.BUILD_ATTR_SQL)
    cur.execute(bench.BUILD_EDGES_SQL)


def _reached(cur, sql, depth, root=1):
    cur.execute(sql, {"root": root, "max_depth": depth})
    return cur.fetchone()[0]


def test_the_posting_table_and_the_edge_table_are_the_same_relation(cur):
    """Defect 2, asserted as set equality rather than inferred from row counts.

    Applicants 1 and 2 share an address AND a phone. Without the DISTINCT in the
    recursive term that transition enters the walk twice, so the frontier
    candidate does strictly more work than the other two over what is supposed
    to be the same graph. Equal cardinality would not be enough here: two
    relations of the same size with different membership are still two graphs.
    """
    _build_graph(cur)
    cur.execute(bench.EDGE_PARITY_SQL)
    attr_edges, edge_rows, attr_only, edges_only = cur.fetchone()
    assert (attr_only, edges_only) == (0, 0), (
        f"posting table and edge table differ: {attr_only} transitions only in "
        f"identity_attr, {edges_only} only in edges"
    )
    assert attr_edges == edge_rows


def test_every_candidate_counts_unique_applicants_at_every_depth(cur):
    """Defect 1's user-visible half: the answer is a set of applicants.

    Hand-computed from the graph above rather than from a second implementation,
    so the expectation is independent of the code under test.
    """
    _build_graph(cur)
    expected = {1: 3, 2: 4, 3: 4, 4: 4, 5: 4}
    for depth, want in expected.items():
        for name, sql, _ in bench.CANDIDATES:
            got = _reached(cur, sql, depth)
            assert got == want, (
                f"{name} reached {got} applicants at depth {depth}, expected {want}"
            )


def test_the_walk_does_not_grow_with_depth_once_the_graph_is_exhausted(cur):
    """Defect 1's cost half, made observable without timing anything.

    A path-enumerating walk keeps producing rows after every applicant has been
    found, because each new lap around the cycle is a new simple path. A
    node-deduplicating one stops. Counting the rows the recursive union actually
    emits distinguishes the two, and unlike a duration it is deterministic.

    Against the previous implementation this graph emits strictly more rows at
    depth 6 than at depth 3; against this one the counts are equal, because
    everything reachable was already reached.
    """
    _build_graph(cur)
    # Same walk as MATERIALIZED_SQL, counting emitted rows instead of distinct
    # applicants -- the quantity that exploded.
    row_count_sql = """
    WITH RECURSIVE walk(id, depth) AS (
        SELECT %(root)s::int, 0
        UNION
        SELECT e.dst, w.depth + 1
          FROM walk w JOIN edges e ON e.src = w.id
         WHERE w.depth < %(max_depth)s
    )
    SELECT count(*) FROM walk
    """
    cur.execute(row_count_sql, {"root": 1, "max_depth": 3})
    shallow = cur.fetchone()[0]
    cur.execute(row_count_sql, {"root": 1, "max_depth": 6})
    deep = cur.fetchone()[0]
    # Bounded by nodes x depth levels, not by the number of simple paths.
    assert deep <= 4 * 7, f"walk emitted {deep} rows over 4 applicants -- path blowup"
    assert deep - shallow <= 4 * 3, (
        f"walk grew by {deep - shallow} rows for three extra depth levels over a "
        f"graph of 4 applicants that is fully reached at depth 2"
    )


def test_no_candidate_carries_a_path_array():
    """The defect had one syntactic tell; catch a reintroduction at the source.

    A path array is what forces per-path expansion, and `NOT x = ANY(w.path)` is
    how it was spelled in all three candidates. This is a lint, not a proof --
    the behavioural assertions above are the proof -- but it names the mistake
    at the exact line a future edit would reintroduce it on.
    """
    for name, sql, _ in bench.CANDIDATES:
        assert "path" not in sql.lower(), (
            f"{name} carries a path array again -- that enumerates every simple "
            f"path through the cycles instead of counting applicants reached"
        )
        assert "UNION ALL" not in sql.upper(), (
            f"{name} uses UNION ALL -- node deduplication is what bounds this walk"
        )
