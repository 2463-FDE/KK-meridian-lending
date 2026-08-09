"""Reproducible benchmark behind adr/0009 (graph store vs. foreign keys).

The first version of that ADR quoted timings from an ad-hoc shell session and
committed nothing, while claiming anyone could re-run it and disagree with
evidence. PR #12's review called that out and was right: an unauditable
measurement is not better than an opinion, it is an opinion with numbers on it.

WHAT THE SECOND VERSION STILL GOT WRONG
    It timed ONE implementation and called the result "PostgreSQL". That
    implementation declared

        WITH RECURSIVE edges AS (<all-pairs self-join>), walk AS (...)

    so every depth timing included planning against the entire graph-shaped
    adjacency relation, even though the production question is root-scoped
    reachability from a single applicant. Measuring the most pessimistic
    relational implementation and reporting it as the relational option's cost
    is how an architecture decision gets made on bad evidence -- the ADR could
    have rejected a perfectly serviceable Postgres design that was never tried.

WHAT THIS VERSION MEASURES
    Three implementations of the SAME traversal over the SAME edge set, so the
    numbers are comparable and the comparison is the point:

      frontier-attr   Expands only the current frontier, joining an indexed
                      attribute-posting table. This is the best relational
                      option: no global relation is ever formed.
      materialized    A derived edge table built once, indexed on src, then
                      walked. Construction is timed SEPARATELY and reported --
                      folding a one-off build into a per-query latency would be
                      the same error in the other direction.
      global-edge     The previous harness, kept only as a pessimistic upper
                      bound. It is labelled as such everywhere it appears and
                      must not be quoted as "Postgres cannot do this".

    Every candidate's reachability count is compared at every depth. If they
    disagree, the run ABORTS: three implementations of different graphs produce
    three meaningless timings, and a silently divergent edge set is exactly the
    defect that made the earlier ssn/ein omission matter. The posting table and
    the edge table are also asserted to be the same relation as SETS before any
    timing is taken (EDGE_PARITY_SQL).

WHAT THE THIRD VERSION GOT WRONG
    It measured PATH ENUMERATION and reported it as reachability. Every
    candidate carried an ARRAY[] of the current path and excluded only nodes
    already on THAT path, so an applicant reachable by k distinct simple paths
    was expanded k times. The identity graph is dense and cyclic -- households,
    shared phones, 200 employers -- so k explodes with depth: the depth-4 run
    emitted 9.4 MILLION recursive rows to arrive at 1,621 distinct applicants,
    and the "depth-4 cliff" that ADR 0009 rested on was the cost of enumerating
    those paths, not the cost of answering the question.

    The question the ADR actually asks is which applicants are reachable within
    d hops. So the walk now deduplicates NODES: `UNION` (not `UNION ALL`) over
    (id, depth), no path array, each node expanded at most once per depth level
    instead of once per path reaching it. Reachability counts are unchanged --
    the same 55/466/850/1,621 at depths 1-4 -- which is the check that this is
    the same question answered a cheaper way, not a different question.

    Note what this does NOT claim: a node found at depth 2 is expanded again at
    depth 3, because a single recursive CTE cannot consult a global visited set.
    Work is bounded by depth x |E| rather than by the number of simple paths.

USAGE
    DATABASE_URL=postgresql://meridian:postgres@localhost:5432/meridian \
        python db/bench/graph_traversal_benchmark.py --explain

    --rows N        population size (default 10000)
    --max-depth D   deepest traversal to attempt (default 5)
    --timeout S     per-query seconds before giving up (default 240)
    --explain       print EXPLAIN (ANALYZE, BUFFERS) for every candidate
    --json PATH     write the single result artifact every published timing
                    must be derived from

ONE RUN IS THE SOURCE
    ADR 0009, services/loan-assistant/app/kg.py and docs/ROADMAP.md previously
    disagreed with each other AND with the table directly above them (3.3s/3.0s
    /72.3s in the table, "1.8s to 43.8s" in the prose, "under two seconds" in
    the decision rule, 44s in kg.py). --json emits the artifact those documents
    are now transcribed from, together with the machine and server settings, so
    a reader can tell which run governs.

DETERMINISM
    No random() anywhere, so two runs on the same Postgres produce identical
    reachability counts. Timings vary with hardware, so the comparison BETWEEN
    the candidates is the finding, not the absolute milliseconds. The synthetic
    population is deliberately sparser than a real fraud ring -- households of
    three, a shared phone every seventh row, 200 employers, identity collisions
    at 1% -- so these numbers are a lower bound on the difficulty.

    ssn and ein are included because adr/0009 decides on a traversal that uses
    them, and they are the highest-signal identity links. Every value is
    synthetic and generated arithmetically ('ssn_' || i/200). No applicant data
    from any real or seeded table is read by this harness.
"""
from __future__ import annotations

import argparse
import json
import os
import platform
import sys
import time

import psycopg2

SCHEMA = "graph_bench"

# Every edge type adr/0009 names. Two applicants are adjacent if they share any
# one of these. Undirected, heterogeneous and cycle-forming -- which is exactly
# why kg.py's fixed-depth foreign-key walks cannot express it.
EDGE_SQL = """
    SELECT a.id AS src, b.id AS dst
      FROM applicants a JOIN applicants b
        ON a.id <> b.id
       AND ( (a.address IS NOT NULL AND a.address = b.address)
          OR (a.phone   IS NOT NULL AND a.phone   = b.phone)
          OR (a.email   IS NOT NULL AND a.email   = b.email)
          OR (a.ssn     IS NOT NULL AND a.ssn     = b.ssn)
          OR (a.ein     IS NOT NULL AND a.ein     = b.ein) )
    UNION
    SELECT ap1.applicant_id, ap2.applicant_id
      FROM applications ap1 JOIN applications ap2
        ON ap1.applicant_id <> ap2.applicant_id
       AND ap1.employer IS NOT NULL AND ap1.employer = ap2.employer
"""

# --- candidate 1: frontier expansion against an indexed posting table --------
# The recursive term touches only the rows adjacent to the current frontier.
# identity_attr is (applicant_id, kind, value) with an index on (kind, value),
# so "who else shares this value" is an index lookup rather than a scan of a
# relation the size of the graph.
#
# A posting table rather than five OR'd column predicates because the edge set
# spans TWO tables -- the employer edge lives on applications -- and PostgreSQL
# forbids a recursive self-reference inside a subquery, so the employer arm
# cannot be expressed as an EXISTS beside the applicant columns. Normalising
# every identity attribute into one relation keeps all six edge kinds in a
# single index-driven join and keeps this candidate's edge set identical to the
# other two, which is the property that makes the comparison mean anything.
FRONTIER_ATTR_SQL = """
WITH RECURSIVE walk(id, depth) AS (
    SELECT %(root)s::int, 0
    UNION
    SELECT DISTINCT x2.applicant_id, w.depth + 1
      FROM walk w
      JOIN identity_attr x1 ON x1.applicant_id = w.id
      JOIN identity_attr x2 ON x2.kind = x1.kind
                           AND x2.value = x1.value
                           AND x2.applicant_id <> w.id
     WHERE w.depth < %(max_depth)s
)
SELECT count(DISTINCT id) AS reached FROM walk
"""

# --- candidate 2: a derived edge table, built once and indexed ---------------
MATERIALIZED_SQL = """
WITH RECURSIVE walk(id, depth) AS (
    SELECT %(root)s::int, 0
    UNION
    SELECT e.dst, w.depth + 1
      FROM walk w JOIN edges e ON e.src = w.id
     WHERE w.depth < %(max_depth)s
)
SELECT count(DISTINCT id) AS reached FROM walk
"""

# --- candidate 3: the global-edge CTE, PESSIMISTIC BASELINE ONLY -------------
# Retained so the cost of the naive formulation is visible. Never quote this as
# the relational option's cost.
#
# It isolates ONE variable: building the whole adjacency relation ONCE PER
# QUERY instead of expanding a frontier. It therefore walks the same
# node-deduplicated way as the other two -- when it did not, it was pessimistic
# for two unrelated reasons at once and the comparison said nothing about
# either. Caught by db/tests/test_graph_traversal_benchmark_counts_nodes.py
# after the first two candidates were fixed and this one was left behind.
#
# AS MATERIALIZED is load-bearing, not decoration. Without it PostgreSQL inlines
# the CTE into the recursive term and rebuilds all 553,928 rows on EVERY
# iteration -- the previous run's depth-5 plan showed `loops=6` on that Append --
# so the candidate measured "one global build per hop", which is neither what it
# claims nor anything an implementation would do. Reviewed on PR #12; asserted
# from the plan now rather than assumed.
GLOBAL_EDGE_SQL = f"""
WITH RECURSIVE edge_rel AS MATERIALIZED ({EDGE_SQL}),
walk(id, depth) AS (
    SELECT %(root)s::int, 0
    UNION
    SELECT e.dst, w.depth + 1
      FROM walk w JOIN edge_rel e ON e.src = w.id
     WHERE w.depth < %(max_depth)s
)
SELECT count(DISTINCT id) AS reached FROM walk
"""

# --- the UNBOUNDED traversal, which is the question the ADR actually poses ---
#
# The three candidates above key their union on (id, depth), so a node is
# deduplicated within a depth level and not across the whole walk. That is the
# right shape for "who is within d hops" and the wrong shape for "who is in this
# ring": drop max_depth from them and the root is rediscovered at depth 2, every
# (same_id, new_depth) pair stays distinct, and the walk never terminates.
# Reviewed on PR #12, and correct -- while `docs/ROADMAP.md` was claiming the
# unbounded question answered.
#
# Dropping `depth` from the row fixes it exactly. The union then deduplicates by
# applicant GLOBALLY, the recursive term stops producing new rows once the
# connected component is exhausted, and the query terminates with no bound at
# all. Distance is not tracked -- it is not part of "who else is in this ring",
# and a query that needs both is a different query with a different cost.
UNBOUNDED_ATTR_SQL = """
WITH RECURSIVE walk(id) AS (
    SELECT %(root)s::int
    UNION
    SELECT DISTINCT x2.applicant_id
      FROM walk w
      JOIN identity_attr x1 ON x1.applicant_id = w.id
      JOIN identity_attr x2 ON x2.kind = x1.kind
                           AND x2.value = x1.value
                           AND x2.applicant_id <> w.id
)
SELECT count(*) AS reached FROM walk
"""

UNBOUNDED_EDGE_SQL = """
WITH RECURSIVE walk(id) AS (
    SELECT %(root)s::int
    UNION
    SELECT e.dst FROM walk w JOIN edges e ON e.src = w.id
)
SELECT count(*) AS reached FROM walk
"""

UNBOUNDED = [
    ("frontier-attr", UNBOUNDED_ATTR_SQL),
    ("materialized", UNBOUNDED_EDGE_SQL),
]

# --- the traversal AS THE ADR STATES IT: reachable applicants AND the path ---
#
# Everything above answers "who is reachable" and throws the route away. The
# question at the top of adr/0009 is "find every other applicant reachable
# through any shared identity attribute, to unbounded depth, AND RETURN THE
# CONNECTING PATH" -- and for the investigator the path is the answer: knowing
# that applicant 8,412 is in the ring is useless without "shares a phone with B,
# who shares an address with C, who shares an employer with the root".
#
# A recursive union cannot carry the path and deduplicate globally at once: the
# path makes every row distinct, so `UNION` stops collapsing anything and the
# walk is back to enumerating simple paths -- the defect this benchmark was
# corrected for twice. So this is the frontier/visited traversal the first
# review actually asked for: expand one level at a time, insert each applicant
# ONCE with the predecessor that found it, and reconstruct routes from those
# predecessors afterwards. Each node is expanded exactly once for the whole
# walk, not once per depth level and certainly not once per path.
#
# Reviewed on PR #12: the published unbounded figure measured `count(*)`, so it
# could not support the ADR's claim.
BFS_TABLE_SQL = """
DROP TABLE IF EXISTS bfs_visited;
CREATE TABLE bfs_visited (
    applicant_id INT PRIMARY KEY,
    depth        INT NOT NULL,
    parent       INT
);
CREATE INDEX ON bfs_visited (depth);
"""

BFS_SEED_SQL = "INSERT INTO bfs_visited (applicant_id, depth, parent) VALUES (%(root)s, 0, NULL)"

# One level. DISTINCT ON keeps exactly one predecessor per newly-found
# applicant; the NOT EXISTS is the visited set, so an applicant already reached
# at a shallower depth is never re-expanded.
BFS_STEP_SQL = """
INSERT INTO bfs_visited (applicant_id, depth, parent)
SELECT DISTINCT ON (e.dst) e.dst, %(depth)s + 1, e.src
  FROM edges e
  JOIN bfs_visited v ON v.applicant_id = e.src AND v.depth = %(depth)s
 WHERE NOT EXISTS (SELECT 1 FROM bfs_visited w WHERE w.applicant_id = e.dst)
 ORDER BY e.dst, e.src
"""

# Every applicant's route back to the root, walked through the predecessor
# column. Bounded by the sum of path lengths rather than by the number of
# paths, which is the whole point of storing one predecessor.
BFS_PATHS_SQL = """
WITH RECURSIVE route(target, node, parent, path) AS (
    SELECT applicant_id, applicant_id, parent, ARRAY[applicant_id]
      FROM bfs_visited
     WHERE parent IS NOT NULL
    UNION ALL
    SELECT r.target, v.applicant_id, v.parent, v.applicant_id || r.path
      FROM route r
      JOIN bfs_visited v ON v.applicant_id = r.parent
)
SELECT target, path FROM route WHERE parent IS NULL
"""

CANDIDATES = [
    ("frontier-attr", FRONTIER_ATTR_SQL,
     "expands only the current frontier against an indexed posting table"),
    ("materialized", MATERIALIZED_SQL,
     "walks a derived edge table built once (construction timed separately)"),
    ("global-edge", GLOBAL_EDGE_SQL,
     "PESSIMISTIC BASELINE -- rebuilds the whole adjacency relation per query"),
]

SETUP_SQL = f"""
DROP SCHEMA IF EXISTS {SCHEMA} CASCADE;
CREATE SCHEMA {SCHEMA};
SET search_path TO {SCHEMA};

CREATE TABLE applicants (
    id SERIAL PRIMARY KEY,
    name TEXT, address TEXT, phone TEXT, email TEXT, ssn TEXT, ein TEXT
);
CREATE TABLE applications (
    id SERIAL PRIMARY KEY,
    applicant_id INT REFERENCES applicants(id),
    employer TEXT
);

INSERT INTO applicants (name, address, phone, email, ssn, ein)
SELECT
    'p' || i,
    'addr_' || (i / 3),
    'ph_'   || (i / 7),
    'e' || i || '@x.test',
    -- 1 percent of the population reuses an SSN with one other applicant:
    -- the synthetic-identity signal, rare and high-value. Synthetic only.
    CASE WHEN i %% 100 = 0 THEN 'ssn_' || (i / 200) END,
    -- entity applicants sharing an EIN, rarer still. Synthetic only.
    CASE WHEN i %% 250 = 0 THEN 'ein_' || (i / 500) END
FROM generate_series(1, %(rows)s) i;

INSERT INTO applications (applicant_id, employer)
SELECT id, 'emp_' || (id %% 200) FROM applicants;

CREATE INDEX ON applicants(address);
CREATE INDEX ON applicants(phone);
CREATE INDEX ON applicants(email);
CREATE INDEX ON applicants(ssn);
CREATE INDEX ON applicants(ein);
CREATE INDEX ON applications(employer);
CREATE INDEX ON applications(applicant_id);
ANALYZE;
"""

# Built ONCE, timed separately, and reported as a build cost rather than hidden
# inside a per-query number.
BUILD_ATTR_SQL = """
CREATE TABLE identity_attr AS
    SELECT id AS applicant_id, 'address' AS kind, address AS value
      FROM applicants WHERE address IS NOT NULL
    UNION ALL SELECT id, 'phone', phone FROM applicants WHERE phone IS NOT NULL
    UNION ALL SELECT id, 'email', email FROM applicants WHERE email IS NOT NULL
    UNION ALL SELECT id, 'ssn',   ssn   FROM applicants WHERE ssn   IS NOT NULL
    UNION ALL SELECT id, 'ein',   ein   FROM applicants WHERE ein   IS NOT NULL
    UNION ALL SELECT applicant_id, 'employer', employer
      FROM applications WHERE employer IS NOT NULL;
CREATE INDEX ON identity_attr (kind, value);
CREATE INDEX ON identity_attr (applicant_id);
ANALYZE identity_attr;
"""

BUILD_EDGES_SQL = f"""
CREATE TABLE edges AS {EDGE_SQL};
CREATE INDEX ON edges (src);
ANALYZE edges;
"""

# The posting table reaches neighbours by joining identity_attr to itself, which
# emits one row per SHARED ATTRIBUTE: two applicants at the same address who also
# share a phone produce that transition twice, while `edges` collapses it with
# UNION. Left alone, the frontier candidate does strictly more work than the
# other two and the three timings are not measuring the same edge relation --
# review finding, visible in the committed plans as 9,419,712 recursive rows
# against 8,486,438.
#
# `SELECT DISTINCT` in the recursive term now collapses parallel transitions
# before they enter the union. This asserts the two relations are identical as
# SETS, in both directions, rather than inferring it from row counts: an equal
# count with different membership would pass a count check and still be two
# different graphs.
EDGE_PARITY_SQL = """
WITH attr_edges AS (
    SELECT DISTINCT x1.applicant_id AS src, x2.applicant_id AS dst
      FROM identity_attr x1
      JOIN identity_attr x2 ON x2.kind = x1.kind
                           AND x2.value = x1.value
                           AND x2.applicant_id <> x1.applicant_id
)
SELECT (SELECT count(*) FROM attr_edges),
       (SELECT count(*) FROM edges),
       (SELECT count(*) FROM (SELECT src, dst FROM attr_edges
                              EXCEPT SELECT src, dst FROM edges) a),
       (SELECT count(*) FROM (SELECT src, dst FROM edges
                              EXCEPT SELECT src, dst FROM attr_edges) b)
"""


def _server_settings(cur) -> dict:
    """The knobs that actually move these timings. Recorded so the ADR's
    revisit threshold is a number somebody can reproduce rather than a number
    that happened on one laptop."""
    out = {}
    cur.execute("SHOW server_version")
    out["server_version"] = cur.fetchone()[0]
    for knob in ("shared_buffers", "work_mem", "effective_cache_size",
                 "max_parallel_workers_per_gather", "jit"):
        cur.execute(f"SHOW {knob}")
        out[knob] = cur.fetchone()[0]
    return out


def _timed(cur, sql: str) -> float:
    t0 = time.monotonic()
    cur.execute(sql)
    return time.monotonic() - t0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rows", type=int, default=10_000)
    ap.add_argument("--max-depth", type=int, default=5)
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--root", type=int, default=1)
    ap.add_argument("--explain", action="store_true")
    ap.add_argument("--json", dest="json_path", default=None)
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2

    conn = psycopg2.connect(url)
    conn.autocommit = True
    artifact: dict = {
        "rows": args.rows,
        "root": args.root,
        "max_depth": args.max_depth,
        "timeout_seconds": args.timeout,
        "machine": {
            "platform": platform.platform(),
            "processor": platform.processor() or platform.machine(),
            "python": platform.python_version(),
        },
        "build": {},
        "candidates": {},
        "explain": {},
    }
    try:
        print(f"building {args.rows} applicants in schema {SCHEMA} ...")
        with conn.cursor() as cur:
            cur.execute(SETUP_SQL, {"rows": args.rows})
            artifact["server"] = _server_settings(cur)
            # Provenance. Every document transcribing these timings names a run;
            # without a timestamp IN the artifact, "the run of 2026-08-07T21:20Z"
            # was a claim about the file rather than something the file said.
            # Read from the server so it cannot disagree with the database the
            # numbers came from.
            cur.execute("SELECT to_char(now() AT TIME ZONE 'UTC', "
                        "'YYYY-MM-DD\"T\"HH24:MI\"Z\"')")
            artifact["run_started_utc"] = cur.fetchone()[0]

            # Build costs, reported on their own. A derived structure that takes
            # a minute to build and answers in milliseconds is a different
            # engineering proposition from one that is free -- and averaging the
            # two into a single "query time" hides precisely that.
            cur.execute(f"SET search_path TO {SCHEMA}")
            artifact["build"]["identity_attr_seconds"] = round(_timed(cur, BUILD_ATTR_SQL), 3)
            artifact["build"]["edges_seconds"] = round(_timed(cur, BUILD_EDGES_SQL), 3)
            cur.execute("SELECT count(*) FROM identity_attr")
            artifact["build"]["identity_attr_rows"] = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM edges")
            artifact["build"]["edge_rows"] = cur.fetchone()[0]

            cur.execute(EDGE_PARITY_SQL)
            attr_edges, edge_rows, attr_only, edges_only = cur.fetchone()
            artifact["edge_parity"] = {
                "attr_edges": attr_edges, "edge_rows": edge_rows,
                "in_attr_not_in_edges": attr_only, "in_edges_not_in_attr": edges_only,
            }
            if attr_only or edges_only:
                print("\n" + "!" * 70)
                print(f"ABORT: the posting table and the edge table are different "
                      f"relations -- {attr_only} transitions only in identity_attr, "
                      f"{edges_only} only in edges.")
                print("Three timings over three graphs are three meaningless "
                      "numbers. Fix the edge sets before publishing anything.")
                print("!" * 70)
                return 4

        print()
        print("one-off build costs (NOT part of any per-query timing below)")
        print(f"  identity_attr : {artifact['build']['identity_attr_seconds']:>8.3f} s  "
              f"({artifact['build']['identity_attr_rows']} rows)")
        print(f"  edges         : {artifact['build']['edges_seconds']:>8.3f} s  "
              f"({artifact['build']['edge_rows']} rows)")
        print()

        header = f"{'depth':>6}  " + "  ".join(f"{name:>16}" for name, _, _ in CANDIDATES)
        print(header)
        print("-" * len(header))

        reached_by_depth: dict = {}
        for name, _, _ in CANDIDATES:
            artifact["candidates"][name] = {"depths": {}}

        for depth in range(1, args.max_depth + 1):
            row = [f"{depth:>6}"]
            for name, sql, _ in CANDIDATES:
                slot = artifact["candidates"][name]["depths"]
                with conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {SCHEMA}")
                    cur.execute(f"SET statement_timeout = {args.timeout * 1000}")
                    t0 = time.monotonic()
                    try:
                        cur.execute(sql, {"root": args.root, "max_depth": depth})
                        reached = cur.fetchone()[0]
                    except psycopg2.errors.QueryCanceled:
                        slot[depth] = {"seconds": None, "reached": None,
                                       "aborted_after_seconds": args.timeout}
                        row.append(f"{'>' + str(args.timeout) + 's abort':>16}")
                        continue
                    elapsed = time.monotonic() - t0
                    slot[depth] = {"seconds": round(elapsed, 3), "reached": reached}
                    row.append(f"{elapsed:>14.2f} s")

                    # Comparability check. Three timings of three different
                    # graphs would be three meaningless numbers.
                    prior = reached_by_depth.setdefault(depth, (name, reached))
                    if prior[1] != reached:
                        print("\n" + "!" * 70)
                        print(f"ABORT: candidates disagree at depth {depth} -- "
                              f"{prior[0]} reached {prior[1]}, {name} reached {reached}.")
                        print("They are not traversing the same graph, so the "
                              "timings cannot be compared. Fix the edge sets "
                              "before publishing any number from this run.")
                        print("!" * 70)
                        return 3
            print("  ".join(row))

        # The unbounded traversal, timed separately because it answers a
        # different question: not "within d hops" but "the whole component".
        print()
        print("unbounded (no depth bound -- terminates when the component is exhausted)")
        artifact["unbounded"] = {}
        unbounded_reached = None
        for name, sql in UNBOUNDED:
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {SCHEMA}")
                cur.execute(f"SET statement_timeout = {args.timeout * 1000}")
                t0 = time.monotonic()
                try:
                    cur.execute(sql, {"root": args.root})
                    reached = cur.fetchone()[0]
                except psycopg2.errors.QueryCanceled:
                    artifact["unbounded"][name] = {
                        "seconds": None, "reached": None,
                        "aborted_after_seconds": args.timeout,
                    }
                    print(f"  {name:>16} : >{args.timeout}s abort")
                    continue
                elapsed = time.monotonic() - t0
            artifact["unbounded"][name] = {"seconds": round(elapsed, 3), "reached": reached}
            print(f"  {name:>16} : {elapsed:>8.3f} s  ({reached} applicants)")
            if unbounded_reached is None:
                unbounded_reached = reached
            elif unbounded_reached != reached:
                print("\n" + "!" * 70)
                print(f"ABORT: unbounded candidates disagree -- {unbounded_reached} "
                      f"vs {reached}. Same edge set, same answer, or neither "
                      f"number means anything.")
                print("!" * 70)
                return 5

        # --- the traversal WITH its connecting path ---------------------------
        print()
        print("unbounded WITH the connecting path (frontier/visited, one "
              "predecessor per applicant)")
        with conn.cursor() as cur:
            cur.execute(f"SET search_path TO {SCHEMA}")
            cur.execute(f"SET statement_timeout = {args.timeout * 1000}")
            t0 = time.monotonic()
            cur.execute(BFS_TABLE_SQL)
            cur.execute(BFS_SEED_SQL, {"root": args.root})
            depth, levels = 0, 0
            while True:
                cur.execute(BFS_STEP_SQL, {"depth": depth})
                if cur.rowcount == 0:
                    break
                depth += 1
                levels += 1
            walk_seconds = time.monotonic() - t0

            t1 = time.monotonic()
            cur.execute(BFS_PATHS_SQL)
            routes = cur.fetchall()
            path_seconds = time.monotonic() - t1

            cur.execute("SELECT count(*), max(depth) FROM bfs_visited")
            visited_rows, max_depth_reached = cur.fetchone()

            # The paths have to be REAL, or this is a fast way to produce
            # fiction. Every consecutive pair on a sample of routes must be an
            # actual edge, the route must start at the root, and it must end at
            # the applicant it claims to reach.
            sample = routes[:: max(1, len(routes) // 200)] if routes else []
            bad = 0
            for target, path in sample:
                if not path or path[0] != args.root or path[-1] != target:
                    bad += 1
                    continue
                pairs = list(zip(path, path[1:]))
                cur.execute(
                    "SELECT count(*) FROM edges e JOIN unnest(%s::int[], %s::int[]) "
                    "AS p(src, dst) ON e.src = p.src AND e.dst = p.dst",
                    ([a for a, _ in pairs], [b for _, b in pairs]),
                )
                if cur.fetchone()[0] != len(pairs):
                    bad += 1

        artifact["unbounded_with_path"] = {
            "walk_seconds": round(walk_seconds, 3),
            "path_reconstruction_seconds": round(path_seconds, 3),
            "total_seconds": round(walk_seconds + path_seconds, 3),
            "reached": visited_rows,
            "levels": levels,
            "max_depth": max_depth_reached,
            "routes_returned": len(routes),
            "routes_sampled": len(sample),
            "routes_invalid": bad,
        }
        print(f"  walk (one predecessor each) : {walk_seconds:>8.3f} s  "
              f"({visited_rows} applicants, {levels} levels, max depth {max_depth_reached})")
        print(f"  path reconstruction        : {path_seconds:>8.3f} s  "
              f"({len(routes)} routes)")
        print(f"  validated                  : {len(sample)} sampled, {bad} invalid")

        if bad:
            print("\n" + "!" * 70)
            print(f"ABORT: {bad} reconstructed route(s) are not real paths through "
                  f"the edge relation. A path benchmark that returns fiction is "
                  f"worse than one that returns nothing.")
            print("!" * 70)
            return 6
        if unbounded_reached is not None and visited_rows != unbounded_reached:
            print("\n" + "!" * 70)
            print(f"ABORT: the path-preserving walk reached {visited_rows} "
                  f"applicants against the count-only walk's {unbounded_reached}. "
                  f"They must traverse the same graph.")
            print("!" * 70)
            return 7

        if args.explain:
            for name, sql, _ in CANDIDATES:
                depths = artifact["candidates"][name]["depths"]
                done = [d for d, v in depths.items() if v.get("seconds") is not None]
                if not done:
                    continue
                deepest = max(done)
                print(f"\nEXPLAIN (ANALYZE, BUFFERS) -- {name} at depth {deepest}:\n")
                with conn.cursor() as cur:
                    cur.execute(f"SET search_path TO {SCHEMA}")
                    cur.execute(f"SET statement_timeout = {args.timeout * 1000}")
                    cur.execute("EXPLAIN (ANALYZE, BUFFERS) " + sql,
                                {"root": args.root, "max_depth": deepest})
                    lines = [line for (line,) in cur.fetchall()]
                artifact["explain"][name] = {"depth": deepest, "plan": lines}
                for line in lines:
                    print("   ", line)

        if args.json_path:
            with open(args.json_path, "w") as fh:
                json.dump(artifact, fh, indent=2)
            print(f"\nresult artifact written to {args.json_path}")
            print("Every timing published in adr/0009, kg.py and docs/ROADMAP.md "
                  "must be transcribed from this file and no other run.")
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
