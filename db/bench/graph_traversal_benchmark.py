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
    defect that made the earlier ssn/ein omission matter.

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
    reachability counts. Timings vary with hardware; the SHAPE (a cliff, not a
    slope) is the finding, not the absolute milliseconds. The synthetic
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
WITH RECURSIVE walk AS (
    SELECT %(root)s::int AS id, 0 AS depth, ARRAY[%(root)s::int] AS path
    UNION ALL
    SELECT x2.applicant_id, w.depth + 1, w.path || x2.applicant_id
      FROM walk w
      JOIN identity_attr x1 ON x1.applicant_id = w.id
      JOIN identity_attr x2 ON x2.kind = x1.kind
                           AND x2.value = x1.value
                           AND x2.applicant_id <> w.id
     WHERE w.depth < %(max_depth)s
       AND NOT x2.applicant_id = ANY(w.path)   -- cycle guard, mandatory
)
SELECT count(DISTINCT id) AS reached FROM walk
"""

# --- candidate 2: a derived edge table, built once and indexed ---------------
MATERIALIZED_SQL = """
WITH RECURSIVE walk AS (
    SELECT %(root)s::int AS id, 0 AS depth, ARRAY[%(root)s::int] AS path
    UNION ALL
    SELECT e.dst, w.depth + 1, w.path || e.dst
      FROM walk w JOIN edges e ON e.src = w.id
     WHERE w.depth < %(max_depth)s
       AND NOT e.dst = ANY(w.path)
)
SELECT count(DISTINCT id) AS reached FROM walk
"""

# --- candidate 3: the global-edge CTE, PESSIMISTIC BASELINE ONLY -------------
# Retained so the earlier published numbers remain reproducible and so the cost
# of the naive formulation is visible. Never quote this as the relational
# option's cost.
GLOBAL_EDGE_SQL = f"""
WITH RECURSIVE edges AS ({EDGE_SQL}),
walk AS (
    SELECT %(root)s::int AS id, 0 AS depth, ARRAY[%(root)s::int] AS path
    UNION ALL
    SELECT e.dst, w.depth + 1, w.path || e.dst
      FROM walk w JOIN edges e ON e.src = w.id
     WHERE w.depth < %(max_depth)s
       AND NOT e.dst = ANY(w.path)
)
SELECT count(DISTINCT id) AS reached FROM walk
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
