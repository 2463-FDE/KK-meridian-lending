"""The agent runtime for the underwriting summary.

The client rejected the previous shape explicitly: application code retrieves,
stuffs the text into a prompt, makes one model call. That is preloaded
retrieval wearing an agent label. What has to be true instead is that **the
model decides to call the tool and the runtime executes it**, and that the
accepted summary is refused if that never happened.

So this module owns two things and no more:

  * a LangChain v1 agent (`create_agent`) over `ChatBedrockConverse`, with
    exactly ONE tool -- `policy_tool.search_underwriting_policy`;
  * `required_tool_was_called()`, which inspects the returned execution state
    for a real tool message and is what the caller gates acceptance on.

**Why the gate reads execution state rather than a flag we set.** A boolean the
application sets after calling the tool itself would pass whether or not the
model ever asked for it -- exactly the failure being designed out. A ToolMessage
in the message history can only exist because the runtime executed a tool call
the model emitted.

The existing application, financial and macro boundaries are NOT tools. The
client asked for one policy/document tool; turning working server-side
retrieval into extra tool calls would inflate the demo and weaken the
guarantees those boundaries already carry (macro fails open, financials are
authenticated server-side). They stay where they are.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
from typing import Any

from . import config, trace
from .policy_tool import TOOL_NAME, search_underwriting_policy

log = logging.getLogger("loan-assistant.agent")


class AgentError(RuntimeError):
    """Base for every way the agent path can refuse.

    Exists so the summary route can have a controlled fallback for an agent
    failure nobody has enumerated yet. Reviewed on PR #63: the first four
    subclasses below all reached the API as a generic 500 `{"detail": "internal
    error"}`, because the route enumerated the `LLM*Error` classes and these are
    not among them. A designed refusal that presents as an internal server error
    is not a refusal contract -- it is the absence of one, and it hides exactly
    the failures this PR added on purpose.

    `test_agent_failures_reach_the_route.py` asserts that every exception this
    module defines inherits from here, so adding a fifth cannot silently
    reintroduce the 500.
    """


class AgentUnavailable(AgentError):
    """The agent runtime could not be constructed.

    Raised rather than falling back to a direct model call. A silent fallback
    would turn the demonstration into the prompt-to-text architecture the client
    rejected, and nothing downstream would show that it had happened.
    """


class AgentStepBudgetExceeded(AgentError):
    """The agent looped past its step budget.

    Refused rather than retried or allowed to continue: an autonomous loop with
    no ceiling is an open line to a paid API, and the client named usage limits
    explicitly.
    """


class PolicyEvidenceMissing(AgentError):
    """The tool ran and found nothing, and the summary was refused for it.

    Distinct from RequiredToolNotCalled on purpose: "never asked" and "asked and
    got nothing" are different failures, and a trace that conflated them could
    not tell an operator which one happened.
    """


class UnsafeTracingConfiguration(AgentError):
    """Tracing was requested but cannot be proven safe, so the run is refused.

    Only reachable if the suppression below cannot be installed. Refusing beats
    running untraced-but-unproven, because the whole point of the guard is that
    nobody has to trust a configuration note.
    """


class AgentTimeout(AgentError):
    """A provider request exceeded the per-attempt transport timeout.

    Restores a boundary this PR had dropped. `call_api` set
    `timeout=TIMEOUT_SECONDS` and mapped `APITimeoutError` to `LLMTimeoutError`,
    which the route renders as **504**. The agent path does not go through
    `call_api`, so nothing bounded a Bedrock call and the 504 became
    unreachable; botocore's own 60s default applied instead.

    **Deliberately not called a deadline.** What the 20 seconds bounds is one
    connect and one read on one HTTP attempt -- not an attempt sequence, not a
    model invocation, not the run. `call_api`'s own "20s" was never a wall
    either: its `@retry(stop_after_attempt(3))` decorator meant three such calls
    plus backoff. Reviewed on PR #63; the wording here says what the code does
    rather than repeating the inherited claim.
    """


class AgentProviderError(AgentError):
    """The provider rejected the call or failed in a way we do not retry.

    Carries the exception CLASS and nothing else. Provider error bodies are on
    the client's prohibited-retention list and can quote the request, so the raw
    text is never put in the message, the log or the HTTP response.
    """


class RequiredToolNotCalled(AgentError):
    """The agent produced an answer without consulting policy.

    Not a warning and not a retry-in-place: the summary is refused. An
    underwriting summary that never looked at the underwriting policy is not a
    weaker version of the product, it is a different one.
    """


#: The agent's contract = the EXISTING summary safety contract, plus the tool
#: requirement. Composed rather than rewritten, and that is the point.
#:
#: The first version of this file wrote a short prompt of its own and silently
#: dropped rules `_SYSTEM` had carried for months -- "use only the data
#: provided, do not invent", no risk tier, no DTI reasoning, no invented numeric
#: threshold. Three of those have deterministic post-validators
#: (`_strip_risk_classifications`, `_strip_dti_claims`,
#: `_strip_contradicting_macro_claims`) so the guarantee survived; "do not
#: invent" has NO deterministic replacement, and dropping it weakened a live
#: safety boundary while adding a feature. Reviewed on PR #63.
#:
#: Composing means the two cannot drift again: editing the summary rules edits
#: what the agent is told, in one place.
def system_prompt() -> str:
    """The summary safety contract plus the policy-tool requirement."""
    from .llm_client import _SYSTEM

    blank = "\n\n"
    return (
        _SYSTEM + blank
        + f"Before answering you MUST call the {TOOL_NAME} tool to find the "
        "underwriting policy that applies to this application, and your summary "
        "must be consistent with what it returns. Do not claim to have consulted "
        "policy without calling the tool." + blank
        + "Return only the requested JSON object."
    )


#: Every environment variable that turns LangChain/LangSmith tracing on.
#: Both spellings are live: `langsmith` reads LANGSMITH_TRACING, and the
#: LANGCHAIN_* names are still honoured for backwards compatibility, so
#: checking one of them would leave the other as an open door.
_TRACING_ENV = ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING")

_TRUTHY = ("1", "true", "yes", "on")

#: Total provider attempts per model invocation, INCLUDING the first.
#:
#: Passed as botocore's `total_max_attempts`, which is the key that means what
#: this name says. `max_attempts` means retries-after-the-first, so the same
#: number there would be four. Verified against botocore 1.43.77, not assumed.
AGENT_TOTAL_PROVIDER_ATTEMPTS = 3


def tracing_is_requested() -> bool:
    """Is LangSmith tracing switched on in this environment?"""
    return any(os.getenv(name, "").strip().lower() in _TRUTHY for name in _TRACING_ENV)


@contextlib.contextmanager
def suppressed_tracing():
    """Run the agent with LangSmith tracing off, whatever the environment says.

    **Interim measure, and deliberately blunt.** Measured on this branch: with
    `LANGSMITH_TRACING=true` and nothing else changed, one agent run posts ~31KB
    to the LangSmith endpoint containing the user prompt, the system prompt, the
    tool query and the retrieved policy text -- four of the categories the client
    put on the prohibited-retention list. That is the default behaviour of the
    framework, not a bug in this code, and no amount of documentation prevents
    someone from setting the variable.

    `LANGSMITH_HIDE_INPUTS`/`HIDE_OUTPUTS` were measured too and do suppress
    those four, leaving ~17KB of structural metadata. They are NOT used here,
    because "the remaining 17KB is safe" is a claim about payloads this branch
    has not enumerated -- provider error bodies in particular are on the
    prohibited list and do not appear on a happy path. Designing and proving a
    redacting emitter is PR B's entire job.

    So the interim guarantee is the one that needs no such claim: the agent path
    transmits nothing. Measured at 0 bytes. PR B replaces this with the
    privacy-safe trace rather than loosening it.
    """
    try:
        from langsmith.run_helpers import tracing_context
    except ImportError as exc:  # pragma: no cover - langsmith is a hard dep
        # No suppressor available. If tracing is off this is harmless; if it is
        # on, we cannot prove anything, so we refuse instead of hoping.
        if tracing_is_requested():
            raise UnsafeTracingConfiguration(
                "LangSmith tracing is enabled but tracing suppression is "
                "unavailable, so the underwriting agent cannot be run safely. "
                "Unset LANGSMITH_TRACING/LANGCHAIN_TRACING_V2."
            ) from exc
        yield
        return

    if tracing_is_requested():
        # Worth saying out loud, because someone has enabled tracing and will
        # not see the framework's usual spans for this path.
        #
        # This used to say `reason=no_privacy_safe_emitter_yet`, which stopped
        # being true when `app/trace.py` landed and was wired into the summary
        # route. Anyone reading it would conclude the summary emits nothing at
        # all, and it emits a privacy-safe run -- so the message contradicted
        # the code (TRC-01).
        log.info("agent framework tracing suppressed stage=privacy_safe "
                 "reason=custom_privacy_safe_emitter_in_use "
                 "emitter=app.trace.summary_trace")
    with tracing_context(enabled=False):
        yield


def build_agent(tools: list | None = None):
    """Construct the LangChain v1 agent over Bedrock.

    Imported lazily so that importing this module -- which the tests and the
    FastAPI app both do -- does not require the framework to be installed or a
    Bedrock client to be constructible. The failure surfaces where the agent is
    actually used, with a message naming what is missing.
    """
    try:
        from langchain.agents import create_agent
        from langchain_aws import ChatBedrockConverse
        from langchain_core.tools import StructuredTool
    except ImportError as exc:  # pragma: no cover - exercised by the import test
        raise AgentUnavailable(
            f"LangChain v1 agent dependencies are not installed: {exc}"
        ) from exc

    if config.LLM_PROVIDER != "bedrock":
        raise AgentUnavailable(
            f"the underwriting agent runs on Bedrock; LLM_PROVIDER is "
            f"{config.LLM_PROVIDER!r}. Set LLM_PROVIDER=bedrock."
        )
    if not config.BEDROCK_MODEL_ID:
        raise AgentUnavailable(
            "BEDROCK_MODEL_ID is not set -- refusing to guess a model id."
        )

    # An explicit transport timeout and attempt limit, because the agent path
    # does not go through `call_api` and inherited neither of the ones that
    # function carried. Left implicit, botocore's own defaults applied: 60s
    # connect, 60s read, and its default attempt count -- per model invocation.
    # Reviewed on PR #63.
    #
    # **These are PER-ATTEMPT transport timeouts, not a deadline for the run.**
    # Measured on botocore 1.43.77: there is no total-deadline knob on Config at
    # all. What 20 seconds bounds is one connect and one read on one HTTP
    # attempt. It does not bound an attempt sequence, a model invocation, or the
    # agent run -- and the first version of this comment claimed it did, having
    # copied the framing from `call_api`, where it was not true either: that
    # function wrapped its 20-second call in
    # `@retry(stop_after_attempt(3), wait_exponential(...))`, so its worst case
    # was three 20-second calls plus backoff, never a 20-second wall.
    #
    # The honest worst case here, stated rather than left to be discovered:
    # AGENT_MAX_STEPS=12 permits 6 model invocations (measured, not derived),
    # each up to 3 attempts, each attempt up to 20s of read -- so roughly six
    # minutes of wall clock before the step budget refuses. No global deadline
    # is invented to make that number smaller; if one is ever required it has to
    # be a real deadline with a test, not a transport timeout relabelled.
    #
    # `total_max_attempts`, NOT `max_attempts`. Verified against the installed
    # botocore rather than assumed: `retries={"max_attempts": 3}` resolves to
    # `{'total_max_attempts': 4}` -- three retries AFTER the initial request.
    # The intent is three attempts in total, so the key that says so is the one
    # used. That difference is 24 provider attempts per summary versus 18.
    from botocore.config import Config as BotocoreConfig

    model = ChatBedrockConverse(
        model=config.BEDROCK_MODEL_ID,
        region_name=config.AWS_REGION or None,
        temperature=0,
        max_tokens=config.AGENT_MAX_OUTPUT_TOKENS,
        config=BotocoreConfig(
            connect_timeout=config.AGENT_REQUEST_TIMEOUT_SECONDS,
            read_timeout=config.AGENT_REQUEST_TIMEOUT_SECONDS,
            retries={"total_max_attempts": AGENT_TOTAL_PROVIDER_ATTEMPTS,
                     "mode": "standard"},
        ),
    )
    # Wrapped HERE rather than decorated at the definition site, so
    # `policy_tool` stays framework-free: its bounds and its allowlist are
    # testable without LangChain installed, and the tool cannot quietly acquire
    # framework behaviour it was not reviewed with. The schema comes from the
    # function's own signature and docstring.
    if tools is None:
        tools = [StructuredTool.from_function(
            func=search_underwriting_policy,
            name=TOOL_NAME,
            description=(
                "Search the client's underwriting policy documents and return up "
                "to 3 short excerpts with their source document, version and "
                "citation. Read-only."
            ),
        )]

    return create_agent(model=model, tools=tools, system_prompt=system_prompt())


def tool_messages(state: Any) -> list:
    """Every tool result in the agent's returned state.

    Tolerant of shape because the state is the framework's, not ours: it may be
    a mapping with "messages" or an object exposing them. What it may NOT be is
    inferred -- if there are no messages, there are no tool calls, and the
    caller refuses.
    """
    messages = []
    if isinstance(state, dict):
        messages = state.get("messages") or []
    else:
        messages = getattr(state, "messages", []) or []

    found = []
    for message in messages:
        # LangChain marks tool results either by class or by a `type` attribute.
        # Checking both avoids depending on one import path staying stable.
        kind = getattr(message, "type", None)
        name = type(message).__name__
        if kind == "tool" or name == "ToolMessage":
            found.append(message)
    return found


def required_tool_was_called(state: Any, tool_name: str = TOOL_NAME) -> bool:
    """Did the RUNTIME execute the required tool?

    A ToolMessage naming the tool exists only because the model emitted a tool
    call and the runtime ran it -- it cannot be produced by application code
    calling the tool itself, nor by the model *saying* it used a tool, which is
    what makes this evidence rather than bookkeeping.

    Says nothing about whether the retrieval found anything. That is
    `policy_evidence_status`, and the two questions are separate on purpose.
    """
    for message in tool_messages(state):
        if getattr(message, "name", None) == tool_name:
            return True
    return False


def policy_evidence_status(state: Any, tool_name: str = TOOL_NAME) -> str:
    """What the policy retrieval actually produced: "hit", "miss" or "absent".

    Reviewed on PR #63 (finding 3). The gate above answers "did a tool run",
    and a tool CAN run and return nothing -- an empty or irrelevant query yields
    `status=miss, hit_count=0`. Treating that as consultation would let an
    ungrounded summary be indistinguishable from a grounded one, which is
    exactly the claim the PR title makes and must therefore hold.

    Returns the strongest status across all calls: a model that misses once and
    then retrieves successfully HAS consulted policy. Categorical by design --
    this is the value the trace records (PR B).
    """
    best = "absent"
    for message in tool_messages(state):
        if getattr(message, "name", None) != tool_name:
            continue
        best = "miss" if best == "absent" else best
        content = getattr(message, "content", "")
        if not isinstance(content, str):
            content = str(content)
        try:
            payload = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            # Unparseable tool output is not evidence of a hit. Fail closed.
            continue
        if isinstance(payload, dict) and payload.get("status") == "hit" \
                and payload.get("hit_count", 0) > 0:
            return "hit"
    return best


def final_text(state: Any) -> str:
    """The last assistant message's text content, or "" if there is none."""
    messages = state.get("messages") if isinstance(state, dict) else getattr(state, "messages", None)
    if not messages:
        return ""
    content = getattr(messages[-1], "content", "")
    if isinstance(content, str):
        return content
    # Some providers return content as a list of blocks.
    parts = []
    for block in content or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif isinstance(block, str):
            parts.append(block)
    return "".join(parts)


#: botocore's timeout classes, matched by NAME rather than imported.
#:
#: Importing them would drag botocore into every import of this module, which
#: the lazy-import design exists to avoid -- the FastAPI app and most of the
#: test suite import `agent` without the AWS stack installed. Names are stable
#: public API; the alternative is a module-level import that breaks the very
#: property `test_importing_agent_needs_no_framework` asserts.
_TIMEOUT_EXCEPTIONS = frozenset({
    "ReadTimeoutError", "ConnectTimeoutError", "ConnectionError",
    "EndpointConnectionError", "ReadTimeout", "ConnectTimeout",
})


def _as_agent_error(exc: BaseException, stage: str = "invoke") -> AgentError:
    """Turn a provider-layer failure into a refusal the route can render.

    Anything not recognised becomes a controlled refusal rather than escaping,
    so a provider exception nobody anticipated is still a mapped status and not
    a generic 500 with the raw error in the log.

    `stage` keeps the two origins distinguishable rather than flattening them.
    A failure while CONSTRUCTING the runtime means the agent could not be made
    ready in this environment, which is what `AgentUnavailable` already means
    and what the route already renders as 503. A failure while INVOKING it is
    the provider refusing a request we managed to send, which is 502. Both keep
    their identifiable timeout category; neither invents one.

    **The message is built from the exception's class name only.** Provider
    error bodies can quote the request that caused them -- which on this path is
    the application prompt -- and raw provider errors are on the client's
    prohibited-retention list. `str(exc)` therefore never appears in the
    message, the log line or the HTTP response.
    """
    if isinstance(exc, AgentError):
        return exc

    name = type(exc).__name__
    if name in _TIMEOUT_EXCEPTIONS:
        log.error("agent provider timeout stage=agent_%s error_class=%s "
                  "timeout_s=%s", stage, name,
                  config.AGENT_REQUEST_TIMEOUT_SECONDS)
        return AgentTimeout(
            f"provider I/O timed out against a "
            f"{config.AGENT_REQUEST_TIMEOUT_SECONDS}s per-attempt transport "
            f"timeout; the summary was not produced"
        )

    if stage == "construct":
        log.error("agent construction failed stage=agent_construct "
                  "error_class=%s", name)
        return AgentUnavailable(
            f"the underwriting agent could not be constructed ({name}); "
            f"no summary was produced"
        )

    log.error("agent provider error stage=agent_invoke error_class=%s", name)
    return AgentProviderError(
        f"the model provider failed ({name}); the summary was not produced"
    )


def run_underwriting_agent(prompt: str, agent=None) -> tuple[str, Any]:
    """Run the agent and return (final text, execution state).

    Returns the state as well as the text on purpose: acceptance is decided by
    the caller from the state, so the state has to travel with the answer rather
    than being summarised into a boolean here.
    """
    # Construction is inside the boundary, not before it. A configuration
    # refusal already raises AgentUnavailable and passes through untouched, but
    # an UNEXPECTED failure from the provider SDK's constructor did not: it
    # escaped to the FastAPI catch-all as a generic 500, and that handler logs
    # the raw exception -- so a constructor error quoting the config was
    # retained verbatim. Reviewed on PR #63 (finding F10).
    if agent is not None:
        runtime = agent
    else:
        try:
            runtime = build_agent()
        except Exception as exc:
            raise _as_agent_error(exc, stage="construct") from exc

    # An explicit, finite budget. LangGraph enforces `recursion_limit` and
    # raises GraphRecursionError, but its default (25) is the framework's
    # choice, not ours, and 25 steps is roughly a dozen model calls for a
    # one-paragraph summary. Reviewed on PR #63 (finding 4).
    #
    # The number is derived, not picked: one model turn to decide, one tool
    # execution, one model turn to answer is 3 steps. The real Bedrock run made
    # three tool calls before answering (7 steps), so the demo genuinely needs
    # more than the minimum. `AGENT_MAX_STEPS` defaults to 12 -- comfortably
    # above observed behaviour, far below anything that could run up a bill --
    # and a loop that exceeds it is refused rather than retried.
    try:
        # Suppression wraps the invoke, not build_agent: tracing attaches per
        # run from the ambient environment, so a guard at construction time
        # would be read once and then be wrong for every later call.
        with suppressed_tracing():
            state = runtime.invoke(
                {"messages": [{"role": "user", "content": prompt}]},
                config={"recursion_limit": config.AGENT_MAX_STEPS},
            )
    except Exception as exc:
        if type(exc).__name__ == "GraphRecursionError":
            log.error("agent exceeded its step budget stage=agent_loop max_steps=%d",
                      config.AGENT_MAX_STEPS)
            trace.record("agent_run", status="refused",
                         refusal_class="AgentStepBudgetExceeded",
                         step_budget=config.AGENT_MAX_STEPS)
            trace.record("model", provider=config.LLM_PROVIDER, status="refused",
                         refusal_class="AgentStepBudgetExceeded",
                         step_budget=config.AGENT_MAX_STEPS)
            raise AgentStepBudgetExceeded(
                f"the agent exceeded {config.AGENT_MAX_STEPS} steps without "
                f"producing an answer; refusing rather than continuing"
            ) from exc
        raise _as_agent_error(exc) from exc

    messages = state["messages"] if isinstance(state, dict) else getattr(state, "messages", [])
    # The runtime as a whole. Declared in `trace.STAGES` from the start and
    # never emitted -- caught in review, and the stage test did not require it,
    # so a declared stage that produced nothing looked exactly like one that
    # worked.
    trace.record(
        "agent_run",
        status="ok",
        tool_calls=len(tool_messages(state)),
        model_turns=sum(1 for m in (messages or []) if getattr(m, "type", "") == "ai"),
        step_budget=config.AGENT_MAX_STEPS,
        provider_attempt_limit=AGENT_TOTAL_PROVIDER_ATTEMPTS,
    )
    trace.record(
        "model",
        provider=config.LLM_PROVIDER,
        model_family="claude" if "claude" in (config.BEDROCK_MODEL_ID or "") else "unknown",
        region=config.AWS_REGION,
        model_turns=sum(1 for m in (messages or []) if getattr(m, "type", "") == "ai"),
        step_budget=config.AGENT_MAX_STEPS,
        provider_attempt_limit=AGENT_TOTAL_PROVIDER_ATTEMPTS,
    )

    calls = [getattr(m, "name", "?") for m in tool_messages(state)]
    # Categorical only: which tools ran and how many times. Never the tool's
    # input, its output, or the model's text.
    log.info("agent run complete tool_calls=%d tools=%s", len(calls), sorted(set(calls)))
    return final_text(state), state
