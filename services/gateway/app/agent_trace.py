"""The trace root: where an agent request is authenticated, and where it starts.

The client asked to see one trace covering UI action → authenticated entry →
agent → retrieval → model → validation → outcome. `loan-assistant`'s trace
covered everything from `request` onward and said so in its own docstring: it
opens **one hop downstream** of the gateway, so the authenticated entry -- the
step that decides whether this request happens at all -- was outside the picture.

This module is that missing root. The gateway authenticates, authorises staff,
and only then opens a run named `gateway_entry` and hands the propagation context
downstream, so the agent's spans attach beneath it.

**The caller does not get to choose the trace.** `langsmith-trace` and `baggage`
are the documented propagation headers, and `_proxy` forwards inbound headers by
default -- so before this, a caller could have supplied its own and parented
Meridian's spans under a tree it controlled. They are stripped on the way in and
replaced with ones minted here. An observability id is not a capability, but a
caller-chosen one lets an outsider group, locate or pollute internal runs.

**What the id is.** A uuid4 from LangSmith's own `RunTree`. Random, opaque,
carries no authorisation, and derived from nothing: not the user, the session,
the application, the applicant, the loan, the amount, an email or the URL. It
identifies a request in a trace and answers no other question.

**What may travel in it.** The same rule as the downstream trace: a closed set of
categorical fields, structurally allowlisted rather than scrubbed. There is no
code path here that can put a prompt, a response, a query, retrieved text, an
identifier or a credential into a run, because nothing but the fields below is
ever attached.
"""
import logging
import os
import uuid

log = logging.getLogger("gateway.agent_trace")

#: The documented LangSmith propagation headers (`RunTree.to_headers()`).
#: Stripped from every inbound request and replaced with the gateway's own.
PROPAGATION_HEADERS = ("langsmith-trace", "baggage")

#: The one stage this service contributes. Named for what actually happened here
#: -- a request arrived, was authenticated, and was authorised -- rather than for
#: the hop it was forwarded to.
STAGE = "gateway_entry"

#: Everything that may be attached to the root run. Closed, and checked rather
#: than trusted: see `_safe`.
ALLOWED_FIELDS = frozenset({
    "stage", "service", "role", "route_class", "tracing_mode", "schema_version",
})

#: Closed vocabularies, so a categorical-looking key cannot carry free text.
VOCABULARIES = {
    "stage": {STAGE},
    "service": {"gateway"},
    "role": {"csr", "underwriter", "admin", "unknown"},
    # The KIND of route, never the path: a path carries an application id.
    #
    # One value, because one route is traced. The client asked to see the
    # underwriting-summary agent end to end; Policy Chat is a separate concern
    # with its own content-retention question, and minting runs for it here
    # would start tracing a path nobody asked to have traced.
    "route_class": {"agent_summary"},
    "tracing_mode": {"privacy_safe_categorical"},
}

SCHEMA_VERSION = "1"


def is_enabled() -> bool:
    """Whether LangSmith tracing is on for this process.

    Read at call time rather than at import: the environment is what the operator
    controls, and a module-level snapshot would ignore a change without saying so.
    """
    flag = os.getenv("LANGSMITH_TRACING", "").strip().lower()
    return flag in ("1", "true", "yes", "on")


def _safe(fields: dict) -> dict:
    """Drop anything not explicitly allowed, and any allowed key whose value is
    outside its vocabulary.

    Structural rather than a scrub: a denylist has to anticipate what it forbids,
    and the thing that leaks is the field nobody thought of.
    """
    clean = {}
    for key, value in fields.items():
        if key not in ALLOWED_FIELDS:
            continue
        if isinstance(value, str):
            vocabulary = VOCABULARIES.get(key)
            if vocabulary is not None and value not in vocabulary:
                continue
            clean[key] = value
        elif isinstance(value, (int, float, bool)):
            clean[key] = value
        # Anything else -- dict, list, object -- is dropped. Those are the shapes
        # that carry payloads.
    return clean


def start_root(role: str | None, route_class: str) -> tuple[dict, object | None]:
    """Open the `gateway_entry` run and return `(headers, run)`.

    `headers` are the documented propagation headers to forward downstream, and
    are empty when tracing is off -- so a disabled deployment forwards nothing
    rather than a dangling context.

    `run` is handed back so the caller can close it once the response status is
    known; `finish_root` does that. Never raises: an observability failure must
    not fail an authenticated request, which is the same rule the downstream
    emitter follows.
    """
    if not is_enabled():
        return {}, None
    try:
        from langsmith.run_trees import RunTree
    except ImportError:  # pragma: no cover - langsmith is a declared dependency
        log.warning("stage=%s status=tracing_unavailable", STAGE)
        return {}, None

    try:
        metadata = _safe({
            "stage": STAGE,
            "service": "gateway",
            "role": (role or "unknown").lower(),
            "route_class": route_class,
            "tracing_mode": "privacy_safe_categorical",
            "schema_version": SCHEMA_VERSION,
        })
        run = RunTree(
            name=STAGE,
            run_type="chain",
            id=uuid.uuid4(),
            # Empty, and structurally so. The request body is the one thing on
            # this hop guaranteed to contain application data.
            inputs={},
            extra={"metadata": metadata},
        )
        run.post()
        return dict(run.to_headers()), run
    except Exception as exc:
        log.warning("stage=%s status=trace_start_failed error_class=%s",
                    STAGE, type(exc).__name__)
        return {}, None


def finish_root(run, http_status: int) -> None:
    """Close the root run with the status the caller actually received.

    The status is the only outcome recorded here. Whether the agent answered, or
    refused, or failed, is the downstream trace's business and is already
    categorical there.
    """
    if run is None:
        return
    try:
        run.end(outputs={"http_status": int(http_status)})
        run.patch()
    except Exception as exc:
        log.warning("stage=%s status=trace_finish_failed error_class=%s",
                    STAGE, type(exc).__name__)
