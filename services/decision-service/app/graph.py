"""LangGraph state graph for the decisioning chain (Week 3).

decide() always ran the same three steps -- pull credit, score, persist -- as a
plain function body. This builds the exact same three steps as an explicit graph
instead: each node calls the same functions decide() always called (_pull_credit,
_run_model, db.transaction), so behavior, every fail-closed exception, and the
existing test suite (which monkeypatches decision.db/decision.httpx/decision.EXPERIAN_KEY
etc.) are unchanged. What the graph buys over the plain function body: each step is
now individually traceable in LangSmith, and each has an explicit node boundary to
extend later (e.g. a retry policy on just the bureau-pull node) instead of editing
inline function code.

References the `decision` module object throughout (never `from .decision import X`
for individual names) -- decision.py's own tests reload that module with
importlib.reload() to test env-var edge cases, which mints new attribute values on
the SAME module object; `decision.py`'s own test comments explain why a static name
import would go stale across a reload. Module-level access here stays correct
across that reload for the same reason.
"""
import json
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


def _node_persist(state: DecisionState) -> dict:
    """Architecture fix (single-writer race closure): decision-service used
    to also write the authoritative `decisions` row here
    (`INSERT ... ON CONFLICT (app_id) DO UPDATE`), unconditionally --
    nothing here knew whether origination-service's `manual_reviews` table
    already held a staff's final decision for this app_id, so a rerun could
    silently overwrite it out from under origination-service's own guards
    (audit finding: an unclosed race between this call and
    review_application's transaction).

    decision-service now only computes and records its OWN append-only
    audit trail (decision_events -- what the model proposed, and why) --
    origination-service is the sole writer of the authoritative `decisions`
    row, under a lock, with a staleness check against manual_reviews (see
    routers/applications.py::run_decision). This function still commits
    decision_events durably before returning: a proposed outcome is never
    reported to the caller without a matching audit row explaining it."""
    application = state["application"]
    result = state["result"]
    bureau_score = state["bureau_score"]
    app_id = application.get("app_id")

    try:
        decision.db.transaction([
            (
                "INSERT INTO decision_events "
                "(app_id, requested_amount, term_months, annual_income, bureau_score, "
                " model_score, model_version, top_features, decision, reason_codes) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    app_id,
                    application.get("requested_amount"),
                    application.get("term_months"),
                    application.get("income"),
                    bureau_score,
                    result["score"],
                    result["model_version"],
                    json.dumps(result["top_features"]),
                    result["decision"],
                    json.dumps(result["reason_codes"]),
                ),
            ),
        ])
    except Exception as e:
        log.error("could not persist decision_event: %s", e)
        raise decision.DecisionPersistenceError(
            f"app_id={app_id}: decision computed ({result['decision']}, score="
            f"{result['score']}) but could not be durably recorded — refusing to "
            "report an outcome with no matching audit trail."
        ) from e

    log.info(
        "GET /decision app_id=%s model_score=%s decision=%s reason_codes=%s",
        app_id, result["score"], result["decision"], result["reason_codes"],
    )
    return {
        "final": {
            "score": result["score"],
            "decision": result["decision"],
            "reason_codes": result["reason_codes"],
            "adverse_action_reason": result["reason_codes"][0] if result["reason_codes"] else None,
        }
    }


_graph = (
    StateGraph(DecisionState)
    .add_node("pull_credit", _node_pull_credit)
    .add_node("score", _node_score)
    .add_node("persist", _node_persist)
    .add_edge("pull_credit", "score")
    .add_edge("score", "persist")
    .add_edge("persist", END)
    .set_entry_point("pull_credit")
    .compile()
)


async def run(application: dict) -> dict:
    state = await _graph.ainvoke({"application": application})
    return state["final"]
