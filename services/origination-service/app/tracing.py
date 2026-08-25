"""Ambient LangSmith tracing, switched off for the auto-offer path.

`app/disclosure_graph.py` runs the two-agent auto-offer flow as a LangGraph
`StateGraph`. LangGraph pulls in `langchain-core`, which instruments every
`invoke` automatically when `LANGSMITH_TRACING` and `LANGSMITH_API_KEY` are set
-- and both are set in every deployed environment here, from the shared `.env`,
pointed at a real project. Nothing had to be wired up for that to happen.

**What travels.** The graph's state is the payload, and `DisclosureState` holds
`app_id`, `decision_inputs` (the approved amount and term for that application)
and the assembled `offer`. An application identifier and a borrower's approved
loan terms are both on the client's prohibited-retention list.

Measured on the sibling service rather than assumed for this one: the identical
mechanism in decision-service posts ~30KB per run with the graph state in it.
The exposure here is narrower than an SSN and it is the same exposure.

**Why suppression rather than a redacting emitter.** The client asked for a trace
of the underwriting AGENT. Nobody asked for an auto-offer trace, and building a
rich one would mean designing and proving a second privacy-safe emitter for a
path whose observability nobody requested. Sending nothing needs no claim about
what a filter would have caught.

**Why `tracing_context` and not the environment.** `tracing_context(enabled=False)`
is LangSmith's documented switch and works through a `ContextVar`, so it is
scoped to the caller that entered it. Clearing `LANGSMITH_TRACING` around a call
instead would be a process-wide mutation racing every concurrent request in this
service -- one suppression window silently disabling another request's tracing,
and the restore re-enabling it mid-flight for anything still running.

This changes no offer, no money math, no persisted row and no response shape. It
changes what leaves the process.
"""
import contextlib
import os

from .logging_config import get_logger

log = get_logger("origination.tracing")

#: Both spellings are live: `langsmith` reads `LANGSMITH_TRACING`, and
#: `langchain-core` still honours the older `LANGCHAIN_TRACING_V2`. Checking one
#: and missing the other is how a service ends up tracing while believing it is
#: not.
_TRACING_ENV = ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING")


class UnsafeTracingConfiguration(RuntimeError):
    """Tracing is on and cannot be suppressed, so the auto-offer is refused."""


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
    on, this raises rather than proceeding. The caller treats a failed auto-offer
    as best-effort (see `auto_generate_offer`), so refusing costs a convenience
    feature and a loan officer can still build the offer manually -- which is a
    smaller price than transmitting an application id and approved loan terms.
    With tracing off there is nothing to suppress and the block simply runs.

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
                "unavailable, so the auto-offer graph cannot run without "
                "transmitting application data. Unset LANGSMITH_TRACING/"
                "LANGCHAIN_TRACING_V2."
            ) from exc
        yield
        return

    if tracing_is_requested():
        # Categorical, and worth saying out loud: someone switched tracing on and
        # is about to see no auto-offer traces. Silence here would look like a
        # broken exporter rather than a deliberate refusal.
        log.warning("auto offer graph tracing suppressed stage=disclosure_graph "
                    "reason=application_data_not_permitted_in_traces")
    with tracing_context(enabled=False):
        yield
