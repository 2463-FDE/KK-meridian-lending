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
express, and a measurement of what that traversal actually costs in PostgreSQL.

**Scope of the measurement.** The benchmark times *reachability* — which
applicants are reachable — in two forms: depth-bounded (within d hops) and
unbounded (the whole connected component, which is the form the question below
actually asks for). It does not time path reconstruction or per-applicant
distance. Both are strictly larger answers, and no number here is evidence
about their cost.

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
WITH RECURSIVE walk(id, depth) AS (
    SELECT :root, 0
    UNION                                      -- deduplicates NODES, not paths
    SELECT DISTINCT x2.applicant_id, w.depth + 1
      FROM walk w
      JOIN identity_attr x1 ON x1.applicant_id = w.id
      JOIN identity_attr x2 ON x2.kind  = x1.kind
                           AND x2.value = x1.value
                           AND x2.applicant_id <> w.id
     WHERE w.depth < :maxdepth
)
SELECT count(DISTINCT id) FROM walk;
```

Two details carry the cost, and getting either wrong is what produced the wrong
answer twice. `UNION` rather than `UNION ALL` is the cycle handling: it
deduplicates rows, so a node is expanded once per depth level rather than once
per simple path reaching it. The earlier version instead carried the path in an
`ARRAY[]` and excluded `ANY(w.path)`, which prunes a row only *after* producing
it — correct output, catastrophic cost in a cyclic graph. The inner `DISTINCT`
collapses parallel transitions: two applicants sharing both an address and a
phone are one edge, and the posting-table join would otherwise emit them twice
while the materialised `edges` relation (built with `UNION`) emits one.

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

So the question is not expressiveness. It is what it costs.

## What it actually costs

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

- **2026-08-09T01:02Z** (`run_started_utc` in the artifact), N = 10,000
  applicants, root = 1, 240 s statement timeout
- PostgreSQL **16.14** — `shared_buffers` 128MB, `work_mem` 4MB,
  `effective_cache_size` 4GB, `max_parallel_workers_per_gather` 2, `jit` on
- Windows 11 (10.0.26200), Intel64 Family 6 Model 140

An earlier revision of this ADR quoted three mutually contradictory sets of
figures — a table reading 3.3 s / 3.0 s / 72.3 s, prose calling the cliff
"1.8 s to 43.8 s", a decision rule promising "under two seconds", and a fourth
number (44 s) in `kg.py`. None of them governed anything, and a fourth and
fifth set have since replaced them for a different reason -- the harness was
measuring the wrong quantity (see below). Everything below is transcribed from the run above and nothing
else.

### Three implementations, because "PostgreSQL is slow" was measuring one bad one

The previous harness declared `WITH RECURSIVE edges AS (<all-pairs self-join>)`
and walked from a single root, so every timing included planning against the
entire adjacency relation — a *global* build charged to a *root-scoped*
question. That is the most pessimistic relational implementation available, and
reporting it as "PostgreSQL" nearly rejected a Postgres design nobody had tried.

The harness now measures three implementations of the **same** traversal over
the **same** edge set. It asserts identical reachability at every depth and
aborts if they diverge, and it asserts that the posting table and the edge table
are the same relation as sets before any timing is taken. On this run that
symmetric difference was **0 in both directions** (553,928 transitions each way)
and all three candidates returned 55 / 466 / 850 / 1,621 / 2,944, so the
comparison is sound.

**Per-query traversal time (seconds), N = 10,000:**

| Depth | Reached | frontier-attr | materialized | global-edge *(pessimistic)* |
|---|---|---|---|---|
| 1 | 55 | **0.002** | 0.001 | 0.56 |
| 2 | 466 | **0.017** | 0.011 | 0.57 |
| 3 | 850 | **0.033** | 0.011 | 0.66 |
| 4 | 1,621 | **0.064** | 0.023 | 0.67 |
| 5 | 2,944 | **0.152** | 0.042 | 0.81 |

One-off build costs, kept out of the per-query numbers on purpose — a derived
structure that takes a second to build and answers in milliseconds is a
different engineering proposition from one that is free:

### The unbounded traversal, which is the one this ADR actually asked about

Everything above is depth-bounded, and the question at the top of this page is
not: *"every applicant reachable ... to unbounded depth"*. Those two are not the
same query, and the depth-bounded form cannot be turned into the unbounded one
by removing the bound. Its union keys on `(id, depth)`, so an applicant is
deduplicated within a level rather than across the walk; drop `max_depth` and
the root is rediscovered at depth 2, every `(same_id, new_depth)` pair is a new
row, and it never terminates. Review finding on PR #12, raised while
`docs/ROADMAP.md` was marking the unbounded question answered.

Removing `depth` from the row is the whole fix. The union then deduplicates by
applicant globally, the recursive term dries up when the connected component is
exhausted, and the query ends on its own with no bound anywhere in it:

```sql
WITH RECURSIVE walk(id) AS (
    SELECT :root
    UNION                       -- global dedupe: no depth in the key
    SELECT e.dst FROM walk w JOIN edges e ON e.src = w.id
)
SELECT count(*) FROM walk;
```

| Traversal | frontier-attr | materialized | Reached |
|---|---|---|---|
| unbounded (whole component) | **0.406 s** | 0.136 s | 10,000 |

On this population every applicant is in one component — households, shared
phones and 200 employers connect the lot — so the unbounded answer is the entire
book of 10,000, found in well under half a second. That is the strongest form of
the finding: not "Postgres keeps up to depth 5", but "Postgres answers the
unbounded question directly".

What it does not return is the connecting path, or the distance to each
applicant. Both are strictly larger answers and neither is measured here.

| Structure | Build | Rows |
|---|---|---|
| `identity_attr` (postings) | 0.238 s | 40,140 |
| `edges` (materialised pairs) | 2.317 s | 553,928 |

The `global-edge` column now runs 0.56–0.81 s, rising gently with depth. Two
costs, and it is worth naming both rather than calling it flat: the 553,928-row
adjacency relation is built **once** per query (that is the constant, and the
variable this candidate exists to isolate), and then each hop performs a full
unindexed `CTE Scan` over it — `loops=5` at depth 5 in the committed plan. A
materialised CTE cannot carry an index, which is precisely the difference
between this candidate and the `materialized` one, where the same relation is a
real table with an index on `src`. So the slope here is the price of having no
index on the walk, and the offset is the build. Reviewed on PR #12; an earlier
revision of this paragraph called the column flat and was wrong about why. Declared without `AS MATERIALIZED`, PostgreSQL inlined the CTE
into the recursive term and rebuilt all 553,928 rows on every iteration (the
previous plan showed `loops=6` on that Append), so the "pessimistic baseline"
was measuring one global build *per hop*. Reviewed on PR #12; the plan is now
asserted in `db/tests/`.

### What that changes, and what it does not

**There is no cliff. The cliff was the benchmark.** A previous revision of this
page reported depth 4 at 17–39 s and depth 5 as not returning inside two
minutes, and built the decision rule on that wall. It was an artifact of how the
harness walked the graph, not a property of PostgreSQL — review finding on
PR #12, confirmed by re-measurement.

Every candidate carried the current path in an `ARRAY[]` and excluded only nodes
already on *that* path, so an applicant reachable by k distinct simple paths was
expanded k times. The identity graph is dense and cyclic — households of three,
a shared phone every seventh row, 200 employers — so k explodes with depth: the
old depth-4 run emitted **9,419,712 recursive rows to yield 1,621 distinct
applicants**. That is the cost of enumerating paths through cycles. It is not
the cost of answering "who is reachable within four hops", which is the only
question the ADR ever asked.

The walk now deduplicates nodes (`UNION` over `(id, depth)`, no path array), so
each node is expanded at most once per depth level. **Same reachability counts,
three orders of magnitude less work:** depth 4 goes from 38.72 s to **0.064 s**,
and depth 5 — which never returned before — answers in **0.152 s**, reaching
2,944 applicants. The identical 55 / 466 / 850 / 1,621 counts at depths 1–4 are
what establishes this is the same question answered a cheaper way.

Even the pessimistic baseline is rescued: building the entire adjacency
relation once per query, then rescanning it per hop, costs 0.56–0.81 s across
depths 1–5. Its old 15–22 s at depth 4 was the same path-enumeration defect,
not the cost of the build.

**What the numbers now say:** on a 10,000-applicant book, PostgreSQL answers
this traversal to depth 5 in under two tenths of a second, and the unbounded
form in under half a second, with no graph store and
no exotic indexing. This ADR has now had its central measurement wrong twice, in
two unrelated ways, and both times the prose reasoned impeccably from it. That
is the argument for keeping the harness — and for the assertions now guarding
it, which is what caught the third candidate still walking the old way after the
first two were fixed (`db/tests/test_graph_traversal_benchmark_counts_nodes.py`).

Ten thousand applicants is a *small* lending book, and the depth at which this
becomes expensive is **not established by this run** — depth 5 was the deepest
measured and it was cheap. A limit would need a run that finds one.

### So the answer changes when all three hold

1. **Traversal is the query, not a step in it.** Reading one loan's history is a
   tree walk; Postgres wins, and no graph store is warranted.
2. **The query is one PostgreSQL cannot express**, rather than one it merely
   runs slowly. Both the depth-bounded and the unbounded reachability queries it
   expresses fine, and answers in milliseconds. Weighted-path computations — FinCEN's 25%-plus-control-person
   rule is one — are where a recursive CTE stops being the natural shape.
3. **The measured latency actually fails a stated requirement.** This is now an
   empirical bar with a number attached, not an assumption: on 10k applicants
   the traversal costs 0.064 s at depth 4, 0.152 s at depth 5, and 0.41 s
   unbounded, so a
   requirement would have to be far tighter than any interactive budget, or the
   book far larger, before this argument carries.

The second and third conditions replace a "depth > 3 is a wall" rule that the
corrected benchmark disproved. If any one of these is false, foreign keys remain
the right answer. **For Meridian today, (1) is false** — there is no fraud-ring
product, no `beneficial_owners` table (Week 9, unbuilt), and the only traversal
in production is `get_loan_history`, which is a depth-4 tree walk from a single
root. The refusal stands, and it now stands on stronger evidence than before:
the previous version refused a graph store while its own table showed Postgres
failing at depth 4.

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
- a production traversal is measured missing its interactive budget on a real
  book size. Stated as "re-measure", not as a depth: the depth-based version of
  this trigger ("depth > 3 at interactive latency") was derived from a benchmark
  that measured path enumeration, depth 5 now costs 0.152 s, and the unbounded
  walk 0.41 s.

If it is revisited, the likely shape is a **derived read model, not a second
source of truth** — the graph projected from Postgres and rebuilt from it, so
the objection in the original justification (two sources of truth for the same
five tables) is answered by construction rather than by refusing the tool.

## Consequences

- **Good:** the refusal is now falsifiable. There is a named query and a number.
  Anyone can re-run the benchmark and disagree with evidence.
- **Good:** the trigger is written down, so Week 9 does not silently inherit a
  decision made before the requirement existed.
- **Good, and learned the hard way twice:** both times this decision was nearly
  made on bad evidence, the fault was in the harness rather than in the
  reasoning — first a global adjacency build charged to a root-scoped question,
  then path enumeration reported as reachability. Both were caught by review, not
  by re-reading the prose. The harness is the artifact worth keeping.
- **Bad, accepted:** if a fraud-ring question arrives before Week 9, there is no
  identity-resolution product to answer it with. That is a missing feature, not
  a performance limit — the traversal underneath it is cheap.
- **Unverified:** measured on synthetic data in a local container, single-user,
  no concurrency, at N = 10,000. Real production numbers would be worse, not
  better — contention and a denser graph both push the same way. Depth 5 is the
  deepest measured; nothing here establishes where the cost becomes prohibitive,
  and no number in this ADR should be read as saying it has been found.
