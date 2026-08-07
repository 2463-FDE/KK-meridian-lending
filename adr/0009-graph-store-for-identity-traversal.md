# ADR 0009: What a graph database would buy that foreign keys do not

- **Status:** Accepted
- **Date:** 2026-08-05
- **Author:** In-house team
- **Supersedes the reasoning in:** `kg.py`'s module docstring, `docs/ROADMAP.md` Week 4

## Context

Week 4 refused Neo4j and recorded why. The justification was:

> the data already IS a graph shape, so standing up a second store would just be
> a second source of truth for the same five tables

The refusal was right. The justification was not an answer, and the Week 1–4
client review said so directly: *"that is also the argument that stops you ever
learning what a graph store buys."* It is unfalsifiable — every relational
schema with foreign keys "is a graph shape", so the argument refuses a graph
store in all cases, including the ones where it is the correct tool.

This ADR replaces it with two specific things: a traversal `kg.py` cannot
express, and a measured point at which the answer flips.

## The traversal kg.py cannot express

`kg.py` has two queries. `get_loan_history` walks
applicant → application → decision → offer. `get_approved_decision_inputs`
walks decision → application. Both share a shape:

- **one root**, supplied by the caller as an `app_id`;
- **fixed depth**, known when the SQL was written;
- **a tree**, following declared foreign keys downward, so no node is reachable
  twice and no cycle is possible.

Every edge is a foreign key someone already modelled. That is what makes them
expressible as joins, and it is also their limit.

The traversal that does not fit:

> **Given one applicant, find every other applicant reachable through any shared
> identity attribute, to unbounded depth, and return the connecting path.**

Concretely: A and B share a home address; B and C work at the same employer; C
and D share a phone number. A and D have nothing whatsoever in common, and are
three hops apart. This is the standard first-party-fraud / synthetic-identity
question — *who else is in this ring* — and every field it needs already exists
in the schema: `applicants.address`, `.phone`, `.email`, `.ssn`, `.ein`, and
`applications.employer`.

It breaks all three of the shape's assumptions at once:

1. **Depth is not known in advance.** The answer to "how many hops" is "however
   many it takes"; a ring is however large it is. A fixed join chain has to
   guess, and guessing wrong under-reports a fraud ring.
2. **The edges are not foreign keys.** They are value equalities on ordinary
   columns, they are undirected, and they are heterogeneous — an
   address edge and an employer edge mean different things and would carry
   different weights in any real scoring.
3. **The graph has cycles.** Households, shared employers and reused phone
   numbers form dense loops, so traversal needs explicit visited-set tracking.
   The tree walks in `kg.py` never need it.

Adding a third fixed query for this would not help. There is no depth to hard-code.

## Postgres can express it — this is the part that matters

The honest answer is not "relational cannot do this". A recursive CTE can, and
the form below is the one to quote — it expands only the current frontier
against an indexed posting table, so no global adjacency relation is ever
formed:

```sql
-- identity_attr(applicant_id, kind, value), indexed on (kind, value) and
-- (applicant_id), built once from applicants.{address,phone,email,ssn,ein}
-- and applications.employer.
WITH RECURSIVE walk AS (
    SELECT :root AS id, 0 AS depth, ARRAY[:root] AS path
    UNION ALL
    SELECT x2.applicant_id, w.depth + 1, w.path || x2.applicant_id
      FROM walk w
      JOIN identity_attr x1 ON x1.applicant_id = w.id
      JOIN identity_attr x2 ON x2.kind  = x1.kind
                           AND x2.value = x1.value
                           AND x2.applicant_id <> w.id
     WHERE w.depth < :maxdepth
       AND NOT x2.applicant_id = ANY(w.path)   -- cycle guard, mandatory
)
SELECT count(DISTINCT id) FROM walk;
```

A posting table rather than five OR'd column predicates because the edge set
spans two tables — the employer edge lives on `applications` — and PostgreSQL
forbids a recursive self-reference inside a subquery, so the employer arm cannot
be bolted on as an `EXISTS` beside the applicant columns. Normalising every
identity attribute into one relation keeps all six edge kinds in a single
index-driven join.

The earlier revision of this ADR printed a different query here: one that
declared `WITH RECURSIVE edges AS (<all-pairs self-join>)` and then walked it.
That version still runs, and is retained in the harness as a labelled
pessimistic baseline, but it is not what a competent implementation would ship
and its timings should never be quoted as PostgreSQL's cost.

So the question is not expressiveness. It is where this stops working.

## The measured flip point

Reproduce with the committed harness. That is the point of the exercise, and
the first version of this ADR failed it -- it quoted an ad-hoc shell session and
committed nothing, while claiming anyone could re-run it (PR #12 review):

```
export DATABASE_URL=postgresql://meridian:postgres@localhost:5432/meridian
python db/bench/graph_traversal_benchmark.py --rows 10000 --max-depth 5
```

`db/bench/graph_traversal_benchmark.py` holds the schema, the index DDL, the
deterministic generator, the exact query and the timing method, and prints an
`EXPLAIN (ANALYZE, BUFFERS)` plan with `--explain`. No `random()` anywhere, so
reachability counts reproduce exactly; timings vary with hardware.

The synthetic population is deliberately *sparser* than a real fraud ring:
households of three sharing an address, a shared phone every seventh row, 200
employers, and identity collisions on `ssn` and `ein` at 1% and 0.4%. Denser
means worse, so these are a lower bound on the problem.

### One run is the source for every number below

All timings on this page come from a single run, recorded as
`db/bench/results.json` with the plans in `db/bench/run-output.txt`:

- **2026-08-07T21:20Z**, N = 10,000 applicants, root = 1, 120 s statement timeout
- PostgreSQL **16.14** — `shared_buffers` 128MB, `work_mem` 4MB,
  `effective_cache_size` 4GB, `max_parallel_workers_per_gather` 2, `jit` on
- Windows 11 (10.0.26200), Intel64 Family 6 Model 140

An earlier revision of this ADR quoted three mutually contradictory sets of
figures — a table reading 3.3 s / 3.0 s / 72.3 s, prose calling the cliff
"1.8 s to 43.8 s", a decision rule promising "under two seconds", and a fourth
number (44 s) in `kg.py`. Since the revisit trigger is latency-based, none of
them governed. Everything below is transcribed from the run above and nothing
else.

### Three implementations, because "PostgreSQL is slow" was measuring one bad one

The previous harness declared `WITH RECURSIVE edges AS (<all-pairs self-join>)`
and walked from a single root, so every timing included planning against the
entire adjacency relation — a *global* build charged to a *root-scoped*
question. That is the most pessimistic relational implementation available, and
reporting it as "PostgreSQL" nearly rejected a Postgres design nobody had tried.

The harness now measures three implementations of the **same** traversal over
the **same** edge set. It asserts identical reachability at every depth and
aborts the run if they diverge; on this run all three returned 55 / 466 / 850 /
1,621, so the comparison is sound.

**Per-query traversal time (seconds), N = 10,000:**

| Depth | Reached | frontier-attr | materialized | global-edge *(pessimistic)* |
|---|---|---|---|---|
| 1 | 55 | **0.005** | 0.002 | 1.325 |
| 2 | 466 | **0.028** | 0.011 | 1.478 |
| 3 | 850 | **0.514** | 0.288 | 2.485 |
| 4 | 1,621 | **38.72** | 16.87 | 19.85 |
| 5 | — | **abort >120 s** | abort >120 s | abort >120 s |

One-off build costs, kept out of the per-query numbers on purpose — a derived
structure that takes seconds to build and answers in milliseconds is a different
engineering proposition from one that is free:

| Structure | Build | Rows |
|---|---|---|
| `identity_attr` (postings) | 0.238 s | 40,140 |
| `edges` (materialised pairs) | 1.690 s | 553,928 |

### What that changes, and what it does not

**It changes the depth ≤3 story completely.** A properly root-scoped traversal
answers depth 3 in **0.29–0.51 s**, not the ~3 s previously published. The old
figure was the cost of rebuilding the whole graph per query. At depth ≤3 there
is not merely "nothing to buy" — there is a comfortable margin, and the earlier
decision rule was nearly violated by its own evidence table.

**It does not change the cliff.** Depth 4 costs 17–39 s on every implementation,
and depth 5 does not return inside two minutes on any of them. Notably the
materialised edge table — a genuine read model, 553,928 rows prebuilt and
indexed — is *not* rescued by that work: 16.87 s at depth 4. The wall is not an
indexing problem.

The `EXPLAIN (ANALYZE, BUFFERS)` says why, and it is structural. At depth 4 the
frontier-attr recursive union emits **9,419,712 rows to yield 1,621 distinct
applicants**, and the materialised variant emits 8,486,438. The index is working
exactly as intended — the plan shows a Bitmap Index Scan returning 4 rows per
probe — so the per-lookup cost is already near optimal. What explodes is the
number of lookups: the walk re-expands every *path* rather than every *node*,
and the cycle guard can only prune a row after it has been produced. A graph
store's index-free adjacency makes a hop a pointer dereference, so its cost
tracks nodes actually visited rather than paths enumerated. Indexing cannot
close that gap, because the index lookup is the thing being repeated.

Ten thousand applicants is a *small* lending book, and the wall is at **depth 4
on a book this size**.

### So the answer flips when all three hold

1. **Traversal is the query, not a step in it.** Reading one loan's history is a
   tree walk; Postgres wins, and no graph store is warranted.
2. **Depth is unbounded and above three.** At depth ≤3 a root-scoped traversal
   answers in **under 0.6 s** on 10k rows (0.514 s worst of the two sound
   implementations), so there is nothing to buy. This threshold is stated
   against the measured frontier-attr column above, not against the pessimistic
   baseline.
3. **It runs interactively.** A nightly batch can absorb the depth-4 cost
   (16.87–38.72 s). An underwriter waiting on a screen cannot, and neither can a
   decision-time check.

If any one of those is false, foreign keys remain the right answer. **For
Meridian today, (1) is false** — there is no fraud-ring product, no
`beneficial_owners` table (Week 9, unbuilt), and the only traversal in
production is `get_loan_history`, which is a depth-4 tree walk from a single
root. The refusal stands.

What changes it: the Week 9 BSA/AML work. Beneficial ownership is inherently
recursive — an entity owned by an entity owned by a person — and FinCEN's
25%-plus-control-person rule is a *weighted path* computation over exactly that
recursion. That is the point to re-open this, and it should be re-opened on the
measurement above rather than on instinct.

## Decision

Keep the shared PostgreSQL schema (ADR 0002) as the single source of truth. Do
not add a graph store now.

Record the trigger to revisit, so this is a decision with an expiry rather than
a permanent refusal:

- an identity-resolution or fraud-ring feature is actually specified; **or**
- beneficial-ownership traversal lands (Week 9) and needs recursive
  weighted-path queries; **or**
- any traversal in production needs depth > 3 at interactive latency.

If it is revisited, the likely shape is a **derived read model, not a second
source of truth** — the graph projected from Postgres and rebuilt from it, so
the objection in the original justification (two sources of truth for the same
five tables) is answered by construction rather than by refusing the tool.

## Consequences

- **Good:** the refusal is now falsifiable. There is a named query and a number.
  Anyone can re-run the benchmark and disagree with evidence.
- **Good:** the trigger is written down, so Week 9 does not silently inherit a
  decision made before the requirement existed.
- **Bad, accepted:** if a fraud-ring question arrives before Week 9, the honest
  answer is a batch job at depth ≤3, which will under-report rings. That is a
  known limitation and is preferable to a 44-second interactive query.
- **Unverified:** the flip point was measured on synthetic data in a local
  container, single-user, no concurrency. Real production numbers would be
  worse, not better — contention and a denser graph both push the same way.
  Treat depth 4 as an upper bound on what works, not a target.
