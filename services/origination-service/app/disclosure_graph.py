"""Multi-agent disclosure assembly (Week 4).

Two-node LangGraph replacing the old direct auto-generate call: one node reads
the knowledge graph (kg.py) for an approved decision's inputs, a second
assembles the disclosure from them -- the exact two-agent shape Week 4's own
prototype design called for (ROADMAP.md: "one agent traverses the KG for an
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
        "annual_rate": 7.99,  # no per-applicant rate exists elsewhere -- same default make_offer() uses
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
    state = _graph.invoke({"app_id": app_id})
    if state.get("skipped"):
        log.warning("auto offer-generation skipped: %s", state["skipped"])
        return None
    return state.get("offer")
