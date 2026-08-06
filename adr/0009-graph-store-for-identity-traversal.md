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
here it is, verified against real PostgreSQL:

```sql
WITH RECURSIVE edges AS (
    SELECT a.id AS src, b.id AS dst
      FROM applicants a JOIN applicants b
        ON a.id <> b.id
       AND ( a.address = b.address OR a.phone = b.phone OR a.email = b.email
          OR a.ssn = b.ssn OR a.ein = b.ein )
    UNION
    SELECT ap1.applicant_id, ap2.applicant_id
      FROM applications ap1 JOIN applications ap2
        ON ap1.applicant_id <> ap2.applicant_id AND ap1.employer = ap2.employer
),
walk AS (
    SELECT :root AS id, 0 AS depth, ARRAY[:root] AS path
    UNION ALL
    SELECT e.dst, w.depth + 1, w.path || e.dst
      FROM walk w JOIN edges e ON e.src = w.id
     WHERE w.depth < :maxdepth
       AND NOT e.dst = ANY(w.path)          -- cycle guard, mandatory
)
SELECT count(DISTINCT id) FROM walk;
```

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

**N = 10,000 applicants, one root, PostgreSQL 16:**

| Depth limit | Applicants reached | Time |
|---|---|---|
| 1 | 55 | 3.3 s |
| 2 | 466 | 1.7 s |
| 3 | 850 | 3.0 s |
| 4 | 1,621 | **72.3 s** |
| 5 | (none) | **aborted at 240 s** |

*An earlier revision reported 795 / 1,159 / 43.8 s. Those came from a CTE that
joined only address, phone, email and employer, while this document claimed
`ssn` and `ein` as edges too, so it measured a sparser graph than the one it
argued about. The review caught it. With the full edge set the walk reaches 40%
further at depth 4 and takes 65% longer, which strengthens the conclusion rather
than changing it.*

Ten thousand applicants is a *small* lending book. The wall is not at some
distant scale — it is at **depth 4 on a book this size**, and it is a cliff, not
a slope: 1.8 s to 43.8 s to no-answer across two hops.

The reason is structural, not a missing index, and the EXPLAIN shows it: at
depth 3 the recursive term produces **160,239 rows to yield 850 distinct
applicants**. The walk re-expands every path rather than every node, and the
cycle guard can only prune a row after it exists. Every hop re-joins through an
index and materialises a new frontier, so cost compounds with the branching
factor: roughly 24× per hop here. A graph store's index-free adjacency makes a
hop a pointer dereference, so its cost tracks the number of nodes actually
*visited* rather than the size of the table being re-searched. Indexing cannot
close that gap, because the index lookup is the thing being repeated.

### So the answer flips when all three hold

1. **Traversal is the query, not a step in it.** Reading one loan's history is a
   tree walk; Postgres wins, and no graph store is warranted.
2. **Depth is unbounded and above three.** At depth ≤3 the recursive CTE answers
   in under two seconds on 10k rows and there is nothing to buy.
3. **It runs interactively.** A nightly batch can absorb 43 seconds. An
   underwriter waiting on a screen cannot, and neither can a decision-time
   check.

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
