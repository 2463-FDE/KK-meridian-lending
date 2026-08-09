"""Knowledge-graph traversal over the shared schema (Week 4).

The borrower -> application -> decision -> offer -> disclosure chain the KG
schema doc describes is already FK-linked relational data in the one shared
Postgres instance every service reads/writes (ADR 0002). There is no separate
graph database backing it, on purpose -- but NOT for the reason this docstring
used to give ("the data already IS a graph shape"). That argument is
unfalsifiable: every schema with foreign keys is a graph shape, so it refuses a
graph store in every case, including the cases where one is correct.

The real reason is measured, in adr/0009: both traversals below are fixed-depth
tree walks from a single root along declared foreign keys, which is what
relational joins are good at. The traversal that would justify a graph store --
"find every applicant reachable from this one through any shared identity
attribute, to unbounded depth" (fraud rings, beneficial ownership) -- cannot be
written here at all, because there is no depth to hard-code. PostgreSQL can
express it with a recursive CTE, and on the single benchmark run ADR 0009 is
transcribed from (db/bench/results.json, 2026-08-09T20:59Z, 10k applicants,
PostgreSQL 16.14) a root-scoped version answers depth 3 in **0.019 s**, depth 4
in **0.050 s** and depth 5 in **0.108 s**. The genuinely unbounded form -- a
recursive union keyed on the applicant alone, which terminates when the
connected component is exhausted -- returns the whole 10,000-applicant
component in **0.32 s**, and a frontier/visited walk returns that component
WITH every connecting path, which is what adr/0009 actually asks for, in
**0.31 s**.

Those numbers replace "16.9-38.7 s at depth 4, no return at depth 5", which
replaced a "44 seconds at depth 4" before that. Both earlier figures were
benchmark defects rather than PostgreSQL's cost: the first rebuilt the entire
adjacency relation on every query, and the second enumerated every simple path
through a cyclic graph instead of counting the applicants actually reached
(9.4M rows to yield 1,621). Deduplicating nodes removed the wall entirely.

So the reason this stays relational is NOT that Postgres cannot keep up -- it
comfortably can, at every depth measured. It is that no production traversal
here needs the query at all. ADR 0009 records the trigger to revisit (Week 9's
beneficial-ownership work is the likely one, and it is a weighted-path problem
rather than a latency one).

This module is the traversal layer -- it reads the same rows the rest of
origination-service already reads, framed as graph nodes/edges instead of ad hoc
joins scattered across routers.

Edges walked here:
  applicant --(applicant_id)--> application
  application --(app_id)--> decision             (decisions.app_id, 1:1)
  application --(app_id)--> decision_events       (decision_events.app_id, 1:many, append-only)
  application --(app_id)--> offer                 (offers.app_id, 1:many)
  decision --(decision_id)--> offer               (offers.decision_id, Week 4's own FK)
"""
from . import db


def get_loan_history(app_id: int) -> dict | None:
    """Walk the full graph for one application: borrower, application, decision
    (+ its audit event), and every offer linked to it. The concrete answer to
    "trace this loan's whole history" the roadmap wanted -- previously only
    answerable by hand-joining five tables per question, not a single call.
    """
    app_rows = db.query(
        "SELECT a.id AS app_id, a.amount, a.term_months, a.purpose, a.status, "
        "       ap.id AS applicant_id, ap.name, ap.email, ap.phone, ap.address "
        "FROM applications a LEFT JOIN applicants ap ON ap.id = a.applicant_id "
        "WHERE a.id = %s",
        (app_id,),
    )
    if not app_rows:
        return None
    application = app_rows[0]

    decision_rows = db.query(
        "SELECT d.outcome, e.model_score, e.model_version, e.reason_codes, "
        "       e.bureau_score, e.occurred_at "
        "FROM decisions d LEFT JOIN decision_events e ON e.app_id = d.app_id "
        "WHERE d.app_id = %s ORDER BY e.occurred_at DESC NULLS LAST LIMIT 1",
        (app_id,),
    )
    decision = decision_rows[0] if decision_rows else None

    offer_rows = db.query(
        "SELECT id, decision_id, fee_pct_used, apr, finance_charge, monthly_payment, "
        "       amount_financed, total_of_payments, created_at "
        "FROM offers WHERE app_id = %s ORDER BY id DESC",
        (app_id,),
    )

    return {
        "applicant": {
            "id": application["applicant_id"], "name": application["name"],
            "email": application["email"], "phone": application["phone"],
            "address": application["address"],
        },
        "application": {
            "app_id": application["app_id"], "amount": application["amount"],
            "term_months": application["term_months"], "purpose": application["purpose"],
            "status": application["status"],
        },
        "decision": decision,
        "offers": offer_rows,
    }


def get_approved_decision_inputs(app_id: int) -> dict | None:
    """What the disclosure-assembly agent needs: the approved decision's own
    linked application inputs (principal/term), found by actually walking the
    decision -> application edge rather than trusting the caller already has
    both rows in hand. Returns None if there is no approve decision on record
    for this app_id -- the graph has no such edge, so there is nothing to
    traverse, and no offer should be generated.
    """
    rows = db.query(
        "SELECT d.app_id, d.outcome, a.amount, a.term_months "
        "FROM decisions d JOIN applications a ON a.id = d.app_id "
        "WHERE d.app_id = %s AND d.outcome = 'approve'",
        (app_id,),
    )
    return rows[0] if rows else None
