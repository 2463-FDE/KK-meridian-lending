"""Multi-agent disclosure assembly (Week 4).

Two-node LangGraph replacing the old direct auto-generate call: one node reads
the knowledge graph (kg.py) for an approved decision's inputs, a second
assembles the disclosure from them -- the exact two-agent shape Week 4's own
prototype design called for (docs/ROADMAP.md: "one agent traverses the KG for an
approved app's decision/offer inputs, a second assembles the disclosure from
them").

Deliberately NOT an LLM doing the math: TILA APR/finance-charge computation
stays the existing deterministic Decimal engine in disclosure-service
(apr.py/schedule.py) -- an LLM approximating regulated dollar math is not an
acceptable trade for "agentic," it's a compliance risk. "Agent" here means a
LangGraph orchestration node with one clear responsibility and a traceable
boundary, not an LLM call; nothing in this file talks to a model.
"""
from typing import TypedDict

from langgraph.graph import END, StateGraph

from . import clients, config, kg
from .logging_config import get_logger
from .tracing import suppressed_tracing

log = get_logger("disclosure_graph")


class DisclosureState(TypedDict, total=False):
    app_id: int
    decision_inputs: dict
    offer: dict
    skipped: str


def _node_kg_reader(state: DisclosureState) -> dict:
    """Agent 1: walk decision -> application for this app_id's approved inputs."""
    inputs = kg.get_approved_decision_inputs(state["app_id"])
    if inputs is None:
        return {"skipped": f"no approve decision on record for app_id={state['app_id']}"}
    return {"decision_inputs": inputs}


def _node_assemble_disclosure(state: DisclosureState) -> dict:
    """Agent 2: hand the KG-derived inputs to disclosure-service's real,
    deterministic offer/TILA engine. This node only orchestrates the call and
    the decision_id link -- it does not compute any of the money math itself."""
    if state.get("skipped"):
        return {}
    inputs = state["decision_inputs"]
    offer = clients.post(clients.DISCLOSURE_URL, "/offers", {
        "application_id": inputs["app_id"],
        "decision_id": inputs["app_id"],  # decisions.app_id is that table's PK
        "principal": float(inputs["amount"]),
        "term_months": inputs["term_months"],
        # The one configured training rate (`config.DEMO_NOTE_RATE_PCT`). This
        # was a literal 7.99 -- the auto-offer path's own copy of a number that
        # also lived in two frontends and two request schemas. No per-applicant
        # rate exists anywhere in this system, by design and for want of any
        # authority for one.
        "annual_rate": config.DEMO_NOTE_RATE_PCT,
    }, headers={"X-Internal-Token": config.INTERNAL_SERVICE_TOKEN})
    return {"offer": offer}


_graph = (
    StateGraph(DisclosureState)
    .add_node("kg_reader", _node_kg_reader)
    .add_node("assemble_disclosure", _node_assemble_disclosure)
    .add_edge("kg_reader", "assemble_disclosure")
    .add_edge("assemble_disclosure", END)
    .set_entry_point("kg_reader")
    .compile()
)


def auto_generate_offer(app_id: int) -> dict | None:
    """Run the two-agent graph for this app_id. Best-effort at the call site
    (see routers/applications.py) -- a disclosure-service hiccup must not fail
    the decision that already happened; the loan officer can still build the
    offer manually via POST /los/offer if this is skipped or fails.
    """
    # Ambient tracing off for the whole graph run. `langgraph` brings
    # `langchain-core`, which traces every `invoke` automatically whenever
    # LANGSMITH_TRACING and LANGSMITH_API_KEY are set -- and both are set in
    # every deployed environment here. The payload is the graph state, which
    # holds the application id, the approved amount and term, and the assembled
    # offer. Nobody asked for an auto-offer trace; see app/tracing.py.
    #
    # Nothing inside the block changed: same nodes, same order, same offer.
    with suppressed_tracing():
        state = _graph.invoke({"app_id": app_id})
    if state.get("skipped"):
        log.warning("auto offer-generation skipped: %s", state["skipped"])
        return None
    return state.get("offer")
