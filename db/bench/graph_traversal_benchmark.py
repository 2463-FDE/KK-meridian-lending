"""Reproducible benchmark behind adr/0009 (graph store vs. foreign keys).

The first version of that ADR quoted timings from an ad-hoc shell session and
committed nothing, while claiming anyone could re-run it and disagree with
evidence. PR #12's review called that out and was right: an unauditable
measurement is not better than an opinion, it is an opinion with numbers on it.

This is the harness. It builds a throwaway schema, generates a deterministic
synthetic population, indexes every join column, and times the recursive CTE at
increasing depth limits.

    DATABASE_URL=postgresql://meridian:postgres@localhost:5432/meridian \
        python db/bench/graph_traversal_benchmark.py

    --rows N        population size (default 10000)
    --max-depth D   deepest traversal to attempt (default 5)
    --timeout S     per-query seconds before giving up (default 240)
    --explain       print the EXPLAIN ANALYZE plan for the deepest completed run

Deterministic by construction -- no random() anywhere -- so two runs on the same
Postgres produce the same reachability counts. Timings will vary with hardware;
the shape (a cliff, not a slope) is the finding, not the absolute milliseconds.

The edge set here matches the traversal adr/0009 actually decides on, including
`ssn` and `ein`. The first benchmark omitted those two, which the review also
caught: they are the highest-signal identity links, so leaving them out
measured a sparser graph than the one being argued about.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

import psycopg2

SCHEMA = "graph_bench"

# Every edge type adr/0009 names. Two applicants are adjacent if they share any
# one of these. Undirected, heterogeneous, and cycle-forming -- which is exactly
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

TRAVERSAL_SQL = f"""
WITH RECURSIVE edges AS ({EDGE_SQL}),
walk AS (
    SELECT %(root)s::int AS id, 0 AS depth, ARRAY[%(root)s::int] AS path
    UNION ALL
    SELECT e.dst, w.depth + 1, w.path || e.dst
      FROM walk w JOIN edges e ON e.src = w.id
     WHERE w.depth < %(max_depth)s
       AND NOT e.dst = ANY(w.path)          -- cycle guard, mandatory
)
SELECT count(DISTINCT id) AS reached FROM walk
"""

# Sharing pattern. Deliberately SPARSER than a real fraud ring -- households of
# three, a shared phone every seventh row, 200 employers, and identity
# collisions (ssn/ein) at 1%, which is generous to Postgres. A denser graph
# makes the numbers worse, not better, so this is a lower bound on the problem.
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
    -- the synthetic-identity signal, rare and high-value.
    CASE WHEN i %% 100 = 0 THEN 'ssn_' || (i / 200) END,
    -- entity applicants sharing an EIN, rarer still
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


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rows", type=int, default=10_000)
    ap.add_argument("--max-depth", type=int, default=5)
    ap.add_argument("--timeout", type=int, default=240)
    ap.add_argument("--root", type=int, default=1)
    ap.add_argument("--explain", action="store_true")
    args = ap.parse_args()

    url = os.environ.get("DATABASE_URL")
    if not url:
        print("DATABASE_URL is required", file=sys.stderr)
        return 2

    conn = psycopg2.connect(url)
    conn.autocommit = True
    try:
        print(f"building {args.rows} applicants in schema {SCHEMA} ...")
        with conn.cursor() as cur:
            cur.execute(SETUP_SQL, {"rows": args.rows})

        print()
        print(f"{'depth':>6}  {'reached':>9}  {'time':>12}")
        print(f"{'-'*6}  {'-'*9}  {'-'*12}")

        deepest_ok = None
        for depth in range(1, args.max_depth + 1):
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {SCHEMA}")
                cur.execute(f"SET statement_timeout = {args.timeout * 1000}")
                t0 = time.monotonic()
                try:
                    cur.execute(TRAVERSAL_SQL, {"root": args.root, "max_depth": depth})
                    reached = cur.fetchone()[0]
                except psycopg2.errors.QueryCanceled:
                    print(f"{depth:>6}  {'--':>9}  {'>' + str(args.timeout) + 's (aborted)':>12}")
                    break
                elapsed = time.monotonic() - t0
                deepest_ok = depth
                print(f"{depth:>6}  {reached:>9}  {elapsed:>10.2f} s")

        if args.explain and deepest_ok:
            print(f"\nEXPLAIN ANALYZE at depth {deepest_ok}:\n")
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {SCHEMA}")
                cur.execute(f"SET statement_timeout = {args.timeout * 1000}")
                cur.execute("EXPLAIN (ANALYZE, BUFFERS) " + TRAVERSAL_SQL,
                            {"root": args.root, "max_depth": deepest_ok})
                for (line,) in cur.fetchall():
                    print("   ", line)
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
