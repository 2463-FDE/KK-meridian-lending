"""LangGraph state graph for the decisioning chain (Week 3).

decide() always ran the same three steps -- pull credit, score, finalize -- as a
plain function body. This builds the exact same three steps as an explicit graph
instead: each node calls the same functions decide() always called (_pull_credit,
_run_model), so behavior, every fail-closed exception, and the existing test suite
(which monkeypatches decision.httpx/decision.EXPERIAN_KEY etc.) are unchanged. What
the graph buys over the plain function body: each step is now individually
traceable in LangSmith, and each has an explicit node boundary to extend later
(e.g. a retry policy on just the bureau-pull node) instead of editing inline
function code.

PR #6 review (Finding 2): the third node used to persist decision_events directly
(via decision.db.transaction) -- it is now compute-only, same as the rest of this
graph; see _node_finalize's own docstring for why, and
routers/applications.py::run_decision on the origination-service side for where
that persistence moved.

References the `decision` module object throughout (never `from .decision import X`
for individual names) -- decision.py's own tests reload that module with
importlib.reload() to test env-var edge cases, which mints new attribute values on
the SAME module object; `decision.py`'s own test comments explain why a static name
import would go stale across a reload. Module-level access here stays correct
across that reload for the same reason.
"""
from typing import TypedDict

from langgraph.graph import END, StateGraph

from . import decision
from .logging_config import get_logger

log = get_logger("decision.graph")


class DecisionState(TypedDict, total=False):
    application: dict
    bureau_score: int
    result: dict
    final: dict


async def _node_pull_credit(state: DecisionState) -> dict:
    bureau_score = await decision._pull_credit(state["application"].get("ssn", ""))
    return {"bureau_score": bureau_score}


async def _node_score(state: DecisionState) -> dict:
    result = await decision._run_model(state["bureau_score"], state["application"])
    return {"result": result}


def _node_finalize(state: DecisionState) -> dict:
    """Architecture fix (PR #6 review, Finding 2): this node used to also
    durably write decision_events here, unconditionally, before decision-
    service ever knew whether the request it's answering had already lost
    a finality race against a staff decision or funding in origination-
    service -- a blocked/discarded rerun still left a permanent-looking
    audit row behind it. decision-service is now fully compute-only: this
    node writes nothing to the database at all. origination-service writes
    decision_events itself, atomically with `decisions`, and ONLY after its
    own lock+recheck confirms this attempt actually wins (see
    routers/applications.py::run_decision) -- so a discarded attempt
    produces no audit row at all, not a misleading one.

    (Earlier architecture fix, still true: decision-service does not write
    the authoritative `decisions` row either -- origination-service is the
    sole writer, under a lock, with a staleness check against
    manual_reviews.)

    Everything decision-service used to persist here is still returned to
    the caller (bureau_score/model_version/top_features alongside the
    existing score/decision/reason_codes) -- origination-service needs all
    of it to write a complete decision_events row on its own end."""
    application = state["application"]
    result = state["result"]
    bureau_score = state["bureau_score"]

    log.info(
        "GET /decision app_id=%s model_score=%s decision=%s reason_codes=%s",
        application.get("app_id"), result["score"], result["decision"], result["reason_codes"],
    )
    return {
        "final": {
            "score": result["score"],
            "decision": result["decision"],
            "reason_codes": result["reason_codes"],
            "adverse_action_reason": result["reason_codes"][0] if result["reason_codes"] else None,
            "bureau_score": bureau_score,
            "model_version": result["model_version"],
            "top_features": result["top_features"],
        }
    }


_graph = (
    StateGraph(DecisionState)
    .add_node("pull_credit", _node_pull_credit)
    .add_node("score", _node_score)
    .add_node("finalize", _node_finalize)
    .add_edge("pull_credit", "score")
    .add_edge("score", "finalize")
    .add_edge("finalize", END)
    .set_entry_point("pull_credit")
    .compile()
)


async def run(application: dict) -> dict:
    state = await _graph.ainvoke({"application": application})
    return state["final"]
