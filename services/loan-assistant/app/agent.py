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

import logging
from typing import Any

from . import config
from .policy_tool import TOOL_NAME, search_underwriting_policy

log = logging.getLogger("loan-assistant.agent")


class AgentUnavailable(RuntimeError):
    """The agent runtime could not be constructed.

    Raised rather than falling back to a direct model call. A silent fallback
    would turn the demonstration into the prompt-to-text architecture the client
    rejected, and nothing downstream would show that it had happened.
    """


class RequiredToolNotCalled(RuntimeError):
    """The agent produced an answer without consulting policy.

    Not a warning and not a retry-in-place: the summary is refused. An
    underwriting summary that never looked at the underwriting policy is not a
    weaker version of the product, it is a different one.
    """


#: What the agent is told. Deliberately short: instructions are not a security
#: control -- the tool's own bounds are (see policy_tool). This says what the
#: job is and that policy must be consulted, and nothing about how to behave if
#: someone tries to talk it out of that, because the enforcement is downstream.
SYSTEM_PROMPT = (
    "You summarise a consumer loan application for an underwriter.\n"
    "\n"
    f"Before answering you MUST call the {TOOL_NAME} tool to find the "
    "underwriting policy that applies to this application, and your summary "
    "must be consistent with what it returns.\n"
    "\n"
    "Return only the requested JSON object. Do not include applicant names, "
    "card numbers, or social security numbers in any field."
)


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

    model = ChatBedrockConverse(
        model=config.BEDROCK_MODEL_ID,
        region_name=config.AWS_REGION or None,
        temperature=0,
        max_tokens=config.AGENT_MAX_OUTPUT_TOKENS,
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

    return create_agent(model=model, tools=tools, system_prompt=SYSTEM_PROMPT)


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

    The whole invariant in one function. A ToolMessage naming the tool exists
    only because the model emitted a tool call and the runtime ran it -- it
    cannot be produced by application code calling the tool itself, which is
    what makes this evidence rather than bookkeeping.
    """
    for message in tool_messages(state):
        if getattr(message, "name", None) == tool_name:
            return True
    return False


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


def run_underwriting_agent(prompt: str, agent=None) -> tuple[str, Any]:
    """Run the agent and return (final text, execution state).

    Returns the state as well as the text on purpose: acceptance is decided by
    the caller from the state, so the state has to travel with the answer rather
    than being summarised into a boolean here.
    """
    runtime = agent if agent is not None else build_agent()
    state = runtime.invoke({"messages": [{"role": "user", "content": prompt}]})

    calls = [getattr(m, "name", "?") for m in tool_messages(state)]
    # Categorical only: which tools ran and how many times. Never the tool's
    # input, its output, or the model's text.
    log.info("agent run complete tool_calls=%d tools=%s", len(calls), sorted(set(calls)))
    return final_text(state), state
