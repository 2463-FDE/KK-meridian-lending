"""A privacy-safe trace of the underwriting agent, and nothing else.

The client's requirement has two halves and the second is the hard one: a trace
that runs from the authenticated request through the agent's decisions, the
retrieval outcome, the model call, deterministic validation and the final
outcome -- carrying **categorical and provenance metadata only**. Prompts, model
responses, queries, retrieved text, application data, identifiers, credentials,
raw provider errors and raw tool payloads must never be retained.

**Why this emits its own runs instead of letting the framework trace.**
Measured on PR #63: with `LANGSMITH_TRACING=true` and nothing else changed, one
agent run posts ~31KB containing the user prompt, the system prompt, the tool
query and the retrieved policy text. That is what the framework does by default,
and `agent.suppressed_tracing()` exists to stop it. Turning it back on and then
filtering would mean the safe path depends on a redactor keeping up with
whatever the framework decides to serialise next -- a losing arrangement, and one
where the failure is silent.

So the suppression stays exactly as it is, the framework still emits zero bytes,
and this module builds the trace explicitly from values the application already
computed. Nothing reaches LangSmith that was not named here on purpose.

**The allow-list is structural, not a scrub.** `_safe()` rejects any key not in
`ALLOWED_FIELDS` and any value that is not a bool, a bounded number, or a string
drawn from a declared vocabulary or matching a declared provenance shape. A
scrubber removes what it recognises; this admits only what it recognises, so a
field added carelessly does not travel -- it raises in tests and is dropped in
production.

**Where the trace actually starts, stated precisely.** The client asked for
"UI/gateway entry through ... final outcome". This trace opens in
loan-assistant's summary route, which is one hop DOWNSTREAM of that: the gateway
is where a session is resolved and where the staff check happens
(`gateway/app/main.py::assistant`), and this service only ever sees an already
forwarded `X-User-Role`. So the first span is the service's own ingress, not
gateway entry, and it is named `request` rather than anything that would imply
otherwise. Extending the trace across the gateway means instrumenting a second
service and propagating a correlation id into this one -- a different concern
than making this path safe, and named as a remaining gap rather than quietly
claimed.

**On identifiers.** The prohibited list includes identifiers, and every run here
carries a `trace_id` and per-span UUIDs. Those are generated in this process for
this request and refer to nothing outside it -- they are not the applicant, the
application, the user or the session. What the list forbids is client and
application identifiers, and none of those travel: the application id is not
recorded anywhere, and the caller appears only as a role. A trace with no id is
not a trace, so the distinction is drawn deliberately rather than by omission.

**What this module deliberately does NOT do.** It does not touch the policy-chat
path or decision-service, both of which still trace their content in full when
`LANGSMITH_TRACING` is set. That is a real exposure, it is named in this PR's
description with evidence, and it is a different concern from making the agent
path safe.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import re
import time
import uuid
from typing import Any

from . import config

log = logging.getLogger("loan-assistant.trace")

#: The stages the client named, in order. A stage outside this set is a
#: programming error rather than something to pass through -- the trace's shape
#: is part of what was agreed, not an emergent property of where calls happen.
STAGES = (
    "request",            # authenticated entry at the API boundary
    "agent_run",          # the LangChain runtime as a whole
    "policy_retrieval",   # the bounded tool's outcome, not its text
    "model",              # the Bedrock call
    "validation",         # the deterministic post-validators
    "outcome",            # what the caller finally received
)

#: Every metadata key that may leave this process. Anything else is dropped.
ALLOWED_FIELDS = frozenset({
    "stage", "service", "outcome", "status", "role",
    "provider", "model_family", "region",
    "tool_name", "tool_calls", "model_turns", "evidence_status",
    "documents", "document_versions", "citations", "hit_count", "top_k",
    "validators_run", "validators_triggered", "refusal_class",
    "http_status", "duration_ms", "step_budget", "provider_attempt_limit",
    "tracing_mode", "schema_version",
})

#: Closed vocabularies. A string field whose value is not listed is dropped,
#: which is what stops free text riding in on a categorical-looking key.
VOCABULARIES = {
    "stage": set(STAGES),
    "service": {"loan-assistant"},
    "status": {"ok", "refused", "error"},
    "outcome": {"summary_returned", "refused", "error"},
    "role": {"csr", "underwriter", "admin", "unknown"},
    "provider": {"bedrock", "anthropic", "unknown"},
    "model_family": {"claude", "unknown"},
    "evidence_status": {"hit", "miss", "absent"},
    "tracing_mode": {"privacy_safe_categorical"},
    "refusal_class": {
        "RequiredToolNotCalled", "PolicyEvidenceMissing", "AgentUnavailable",
        "AgentStepBudgetExceeded", "UnsafeTracingConfiguration",
        "AgentTimeout", "AgentProviderError",
        "LLMInsufficientDataError", "LLMCostGuardError", "LLMTimeoutError",
        "LLMResponseError",
        # Reached before the agent runs at all: the route could not assemble
        # the application. Recorded so an early exit is a described outcome
        # rather than a trace that simply stops.
        "application_not_found", "forbidden", "upstream_unavailable",
        "none",
    },
    "validators_run": {"macro_contradiction", "risk_classification", "dti_claim"},
    "validators_triggered": {"macro_contradiction", "risk_classification", "dti_claim"},
}

#: `region` is AWS infrastructure, not client data, but it is still matched
#: rather than trusted so a hostname or an ARN cannot arrive in its place.
_REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-\d$")

#: Policy-corpus provenance. These identify a document in the client's OWN
#: published policy, never an applicant: a filename from the tool's allowlist,
#: a content hash, or `chunk_id (sha256:...)`. Constrained by shape so a
#: retrieved excerpt cannot be posted as a "citation".
_DOCUMENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}\.md$")
_VERSION_RE = re.compile(r"^sha256:[0-9a-f]{6,32}$")
_CITATION_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}\.md#\d+\.\d+ \(sha256:[0-9a-f]{6,32}\)$")

#: A tool name is a Python identifier chosen in this repository, never
#: anything a caller or a model supplies. Shaped rather than vocabularied so a
#: second tool does not require editing this file to become observable, while
#: still refusing anything that is not an identifier.
_TOOL_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")

_SHAPED = {
    "tool_name": _TOOL_NAME_RE,
    "region": _REGION_RE,
    "documents": _DOCUMENT_RE,
    "document_versions": _VERSION_RE,
    "citations": _CITATION_RE,
}

#: Integer fields, with ceilings. A count is categorical; an unbounded integer
#: could still carry an amount or an identifier, so each is range-checked.
_BOUNDED_INTS = {
    "tool_calls": 100, "model_turns": 100, "hit_count": 100, "top_k": 100,
    "http_status": 599, "duration_ms": 3_600_000,
    "step_budget": 1000, "provider_attempt_limit": 100,
}

#: Bumped when the shape of what is emitted changes, so a consumer can tell a
#: v1 trace from a later one instead of guessing.
SCHEMA_VERSION = "1"

_MAX_LIST = 10


def _safe_scalar(key: str, value: Any):
    """Return the value if this key is allowed to carry it, else None."""
    if isinstance(value, bool):
        return value
    if key in _BOUNDED_INTS:
        if isinstance(value, int) and 0 <= value <= _BOUNDED_INTS[key]:
            return value
        return None
    if isinstance(value, str):
        vocabulary = VOCABULARIES.get(key)
        if vocabulary is not None:
            return value if value in vocabulary else None
        shape = _SHAPED.get(key)
        if shape is not None:
            return value if shape.match(value) else None
        # A string key with neither a vocabulary nor a shape has not been
        # reviewed, so it does not travel.
        return None
    return None


def _safe(fields: dict) -> dict:
    """Admit only reviewed keys carrying reviewed values.

    Fails closed in both directions: an unknown key is dropped even if its
    value looks harmless, and a known key is dropped if its value is not one
    of the forms declared for it. Lists are bounded and filtered element by
    element, because "citations" is the one place a caller could hand over an
    arbitrary amount of text by accident.
    """
    clean = {}
    for key, value in (fields or {}).items():
        if key not in ALLOWED_FIELDS:
            continue
        if isinstance(value, (list, tuple, set)):
            admitted = []
            for item in sorted(value, key=str)[:_MAX_LIST]:
                got = _safe_scalar(key, item)
                if got is not None:
                    admitted.append(got)
            if admitted:
                clean[key] = admitted
            continue
        got = _safe_scalar(key, value)
        if got is not None:
            clean[key] = got
    return clean


class _Span:
    """One stage, holding only what `_safe` admitted."""

    __slots__ = ("name", "fields", "started_at", "ended_at", "id")

    def __init__(self, name: str, fields: dict):
        self.id = str(uuid.uuid4())
        self.name = name
        self.fields = fields
        self.started_at = time.time()
        self.ended_at: float | None = None


class SummaryTrace:
    """The spans for one summary request.

    Accumulated in memory and shipped once at the end rather than streamed:
    the final outcome is what makes the earlier stages meaningful, and a
    half-trace from a crashed request is worse than none for an operator trying
    to tell a refusal from a failure.
    """

    def __init__(self):
        self.id = str(uuid.uuid4())
        self.spans: list[_Span] = []
        self.started_at = time.time()
        self._open: dict[str, _Span] = {}

    def record(self, stage: str, **fields) -> None:
        """Add a completed stage."""
        if stage not in STAGES:
            log.debug("trace: unknown stage=%s ignored", stage)
            return
        span = _Span(stage, _safe(dict(fields, stage=stage)))
        span.ended_at = span.started_at
        self.spans.append(span)

    @contextlib.contextmanager
    def timed(self, stage: str, **fields):
        """A stage whose duration matters. Duration is categorical enough to
        keep: it describes the system, not the applicant."""
        if stage not in STAGES:
            yield
            return
        span = _Span(stage, _safe(dict(fields, stage=stage)))
        self.spans.append(span)
        try:
            yield span
        finally:
            span.ended_at = time.time()
            span.fields.update(_safe(
                {"duration_ms": int((span.ended_at - span.started_at) * 1000)}))

    def annotate(self, stage: str, **fields) -> None:
        """Add fields to the most recent span with this name."""
        for span in reversed(self.spans):
            if span.name == stage:
                span.fields.update(_safe(fields))
                return

    def payload(self) -> dict:
        """The whole trace, re-filtered.

        Filtered a second time on the way out on purpose. `record` already
        filtered, but a later `annotate` or a future caller reaching into
        `span.fields` would bypass that, and the guarantee has to hold at the
        boundary that actually transmits.
        """
        return {
            "name": "underwriting_summary",
            "trace_id": self.id,
            "schema_version": SCHEMA_VERSION,
            "tracing_mode": "privacy_safe_categorical",
            "spans": [
                {
                    "id": span.id,
                    "name": span.name,
                    "duration_ms": int(((span.ended_at or span.started_at)
                                        - span.started_at) * 1000),
                    "metadata": _safe(span.fields),
                }
                for span in self.spans
            ],
        }


_current: contextvars.ContextVar[SummaryTrace | None] = contextvars.ContextVar(
    "loan_assistant_summary_trace", default=None)


def current() -> SummaryTrace | None:
    """The trace for the request on this task, if one is open.

    A context variable rather than a parameter threaded through five call
    sites: the alternative changes the signature of `summarize_application`,
    `_summary_text_via_agent` and `run_underwriting_agent` so that observability
    appears in the type of every function it observes, and every test that
    calls them directly has to grow an argument it does not care about.
    """
    return _current.get()


def record(stage: str, **fields) -> None:
    """Record on the current trace, or do nothing if none is open."""
    trace = current()
    if trace is not None:
        trace.record(stage, **fields)


def annotate(stage: str, **fields) -> None:
    trace = current()
    if trace is not None:
        trace.annotate(stage, **fields)


@contextlib.contextmanager
def timed(stage: str, **fields):
    trace = current()
    if trace is None:
        yield None
        return
    with trace.timed(stage, **fields) as span:
        yield span


def is_enabled() -> bool:
    """Emit only when tracing is switched on AND an endpoint is configured.

    Deliberately the same switch operators already use. The safety of this path
    does not come from the switch being off -- it comes from what `_safe`
    admits -- so gating on a private variable would only mean the safe trace is
    the one nobody turns on.
    """
    from .agent import tracing_is_requested

    return tracing_is_requested() and bool(config.LANGSMITH_API_KEY)


@contextlib.contextmanager
def summary_trace(role: str | None = None):
    """Open a trace for one summary request and emit it at the end."""
    trace = SummaryTrace()
    token = _current.set(trace)
    trace.record("request", service="loan-assistant",
                 role=(role or "unknown").lower(),
                 tracing_mode="privacy_safe_categorical")
    try:
        yield trace
    finally:
        _current.reset(token)
        try:
            emit(trace)
        except Exception as exc:  # pragma: no cover - emission must never fail a request
            # Categorical, and never the exception text: an emitter failure can
            # carry the payload it failed to send.
            log.warning("trace emission failed stage=trace_emit error_class=%s",
                        type(exc).__name__)


def emit(trace: SummaryTrace) -> None:
    """Ship the trace to LangSmith, if enabled.

    One run with the stages as child runs. `inputs` is always empty and
    `outputs` carries only the filtered payload -- there is no code path here
    that can put a prompt or a response in either.
    """
    if not is_enabled():
        return
    payload = trace.payload()
    try:
        from langsmith import Client
    except ImportError:  # pragma: no cover - langsmith is a hard dependency
        return

    client = Client()
    run_id = uuid.UUID(trace.id)
    started = trace.started_at
    client.create_run(
        name="underwriting_summary",
        run_type="chain",
        id=run_id,
        inputs={},
        extra={"metadata": {"schema_version": SCHEMA_VERSION,
                            "tracing_mode": "privacy_safe_categorical"}},
        start_time=_dt(started),
    )
    for span in payload["spans"]:
        client.create_run(
            name=span["name"],
            run_type="chain",
            id=uuid.UUID(span["id"]),
            parent_run_id=run_id,
            inputs={},
            outputs=span["metadata"],
            extra={"metadata": span["metadata"]},
            start_time=_dt(started),
        )
        client.update_run(uuid.UUID(span["id"]), outputs=span["metadata"],
                          end_time=_dt(started + span["duration_ms"] / 1000))
    client.update_run(run_id, outputs=payload, end_time=_dt(time.time()))


def _dt(epoch: float):
    import datetime

    return datetime.datetime.fromtimestamp(epoch, tz=datetime.timezone.utc)
