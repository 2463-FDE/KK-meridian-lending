"""Ambient LangSmith tracing, switched off for the decision path.

`app/graph.py` runs the credit decision as a LangGraph `StateGraph`. LangGraph
pulls in `langchain-core`, which instruments every `ainvoke` automatically when
`LANGSMITH_TRACING` and `LANGSMITH_API_KEY` are set -- and both are set in every
deployed environment here, from the shared `.env`, pointed at a real project.
Nothing had to be wired up for that to happen, which is exactly why it went
unnoticed: the ROADMAP records it as tracing that "comes for free".

**What it was actually sending.** Measured against a local sink rather than
argued about: one decision posts ~30KB, and the graph's state is the payload.
`DecisionState.application` is the whole application dict -- and
`_node_pull_credit` reads `application["ssn"]`, so the SSN is in it -- alongside
`bureau_score`, `bureau_reference_id` and the model result. The sentinels for
SSN, bureau score, bureau reference id, applicant name, application id, income
and the bureau request key all came back present in the posted bytes.

Every one of those is on the client's prohibited-retention list, and the list is
not the only reason this matters: an SSN and a credit score leaving the estate for
a third-party SaaS is a different category of problem from a verbose log.

**Why suppression rather than a redacting emitter.** The client asked for a trace
of the AGENT. Nobody asked for a decision-service trace, and building a rich one
here would mean designing a second privacy-safe emitter -- and proving it -- for
a path whose observability nobody requested. The cheaper and more honest move is
to send nothing, which needs no claim about what a filter would have caught.
`loan-assistant/app/agent.py` reached the same conclusion for the same reason and
uses the same mechanism.

**Why `tracing_context` and not the environment.** `tracing_context(enabled=False)`
is LangSmith's documented switch and it works through a `ContextVar`, so it is
scoped to the task that entered it. Clearing `LANGSMITH_TRACING` around a request
instead would be a process-wide mutation racing every concurrent request in this
service: one decision's suppression window would silently disable another's
tracing, and restoring the variable afterwards would re-enable it mid-flight for
anything still running. A contextvar has neither problem.

This changes no scoring, no decision, no persisted row and no response shape. It
changes what leaves the process.
"""
import contextlib
import os

from .logging_config import get_logger

log = get_logger("decision.tracing")

#: Both spellings are live: `langsmith` reads `LANGSMITH_TRACING`, and
#: `langchain-core` still honours the older `LANGCHAIN_TRACING_V2`. Checking one
#: and missing the other is how a service ends up tracing while believing it is
#: not.
_TRACING_ENV = ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING")


class UnsafeTracingConfiguration(RuntimeError):
    """Tracing is on and cannot be suppressed, so the decision is refused."""


def tracing_is_requested() -> bool:
    """Whether any of the tracing switches is on.

    Read at call time, not at import: the value an operator set is the value that
    should apply, and a module-level snapshot would ignore a change silently.
    """
    return any(os.getenv(name, "").strip().lower() in ("1", "true", "yes", "on")
               for name in _TRACING_ENV)


@contextlib.contextmanager
def suppressed_tracing():
    """Run the enclosed block with ambient LangSmith tracing off.

    Fails closed. If the suppressor cannot be imported while tracing is switched
    on, this raises rather than proceeding: the alternative is a decision that
    quietly ships an SSN to a third party, and an unavailable dependency is not a
    reason to accept that. With tracing off there is nothing to suppress, so the
    same missing import is harmless and the block simply runs.

    In practice the import cannot fail while `langgraph` is installed -- it
    depends on `langsmith` transitively, which is precisely why the tracing was
    on in the first place. The branch is kept because "it cannot happen" is a
    claim about a dependency tree that changes without asking.
    """
    try:
        from langsmith.run_helpers import tracing_context
    except ImportError as exc:  # pragma: no cover - langsmith is transitive
        if tracing_is_requested():
            raise UnsafeTracingConfiguration(
                "LangSmith tracing is enabled but tracing suppression is "
                "unavailable, so the credit decision cannot run without "
                "transmitting application data. Unset LANGSMITH_TRACING/"
                "LANGCHAIN_TRACING_V2."
            ) from exc
        yield
        return

    if tracing_is_requested():
        # Categorical, and worth saying out loud: someone switched tracing on and
        # is about to see no decision traces. Silence here would look like a
        # broken exporter rather than a deliberate refusal.
        log.warning("decision graph tracing suppressed stage=decision_graph "
                    "reason=application_data_not_permitted_in_traces")
    with tracing_context(enabled=False):
        yield
