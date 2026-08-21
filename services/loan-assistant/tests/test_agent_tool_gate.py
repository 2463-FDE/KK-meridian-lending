"""A summary is accepted only if the RUNTIME executed the policy tool.

The client rejected preloaded retrieval: application code retrieves, pastes the
text into a prompt, makes one model call, and the result wears an agent label.
The defence against that returning by accident is not the system prompt -- a
model can ignore an instruction, and a happy-path demo would still look green
while having quietly become prompt-to-text.

So acceptance is gated on the agent's own execution state. A ToolMessage naming
the tool exists only because the model emitted a tool call and the runtime ran
it; application code calling the tool itself produces no such message. That
asymmetry is the whole test file.

No paid calls here. The agent is a fake whose only job is to return a state with
or without a tool message -- the real Bedrock run is demo evidence (PR C), not a
CI dependency.
"""
import json

import pytest

from app import agent, llm_client, policy_tool


class _FakeToolMessage:
    """Shaped like a LangChain ToolMessage for the two fields the gate reads."""

    type = "tool"

    def __init__(self, name, content=""):
        self.name = name
        self.content = content


class _FakeAIMessage:
    type = "ai"

    def __init__(self, content):
        self.content = content


SUMMARY_JSON = json.dumps({
    "loan_amount": 18000, "term_months": 48, "purpose": "debt consolidation",
    "summary": "Adequate income for the requested amount.", "flags": [],
})


def _state(messages):
    return {"messages": messages}


# --------------------------------------------------------------------------
# The gate itself.
# --------------------------------------------------------------------------

def test_a_runtime_tool_message_is_recognised():
    state = _state([_FakeToolMessage(policy_tool.TOOL_NAME, "{}"),
                    _FakeAIMessage(SUMMARY_JSON)])

    assert agent.required_tool_was_called(state) is True


def test_no_tool_message_means_the_tool_was_not_called():
    state = _state([_FakeAIMessage(SUMMARY_JSON)])

    assert agent.required_tool_was_called(state) is False


def test_a_different_tool_does_not_satisfy_the_gate():
    """Someone adding a second tool later must not accidentally satisfy the
    policy requirement with it."""
    state = _state([_FakeToolMessage("some_other_tool", "{}"),
                    _FakeAIMessage(SUMMARY_JSON)])

    assert agent.required_tool_was_called(state) is False


def test_an_empty_state_is_not_treated_as_success():
    """Fail closed. An unexpected state shape must not read as 'tool called'."""
    assert agent.required_tool_was_called({}) is False
    assert agent.required_tool_was_called({"messages": []}) is False
    assert agent.required_tool_was_called(object()) is False


# --------------------------------------------------------------------------
# The gate as the summary path enforces it.
# --------------------------------------------------------------------------

def test_a_summary_is_accepted_when_the_runtime_called_the_tool(monkeypatch):
    monkeypatch.setattr(agent, "run_underwriting_agent",
                        lambda prompt: (SUMMARY_JSON,
                                        _state([_FakeToolMessage(policy_tool.TOOL_NAME),
                                                _FakeAIMessage(SUMMARY_JSON)])))

    assert llm_client._summary_text_via_agent("prompt") == SUMMARY_JSON


def test_a_summary_is_refused_when_the_model_skipped_the_tool(monkeypatch):
    """The anti-happy-path case.

    A model that answers straight from the prompt produces a perfectly
    well-formed summary. Without this gate the demo passes and the architecture
    has silently become the one the client rejected.
    """
    monkeypatch.setattr(agent, "run_underwriting_agent",
                        lambda prompt: (SUMMARY_JSON, _state([_FakeAIMessage(SUMMARY_JSON)])))

    with pytest.raises(agent.RequiredToolNotCalled):
        llm_client._summary_text_via_agent("prompt")


def test_the_refusal_logs_no_model_content(monkeypatch, caplog):
    """The refusal is the most tempting place to log what the model said."""
    import logging

    monkeypatch.setattr(agent, "run_underwriting_agent",
                        lambda prompt: ("SENTINEL-MODEL-TEXT-9001",
                                        _state([_FakeAIMessage("SENTINEL-MODEL-TEXT-9001")])))
    with caplog.at_level(logging.DEBUG):
        with pytest.raises(agent.RequiredToolNotCalled):
            llm_client._summary_text_via_agent("SENTINEL-PROMPT-9002")

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "SENTINEL-MODEL-TEXT-9001" not in logged
    assert "SENTINEL-PROMPT-9002" not in logged
    assert policy_tool.TOOL_NAME in logged, "the refusal should say which tool was required"


def test_the_agent_path_does_not_fall_back_to_a_direct_call(monkeypatch):
    """Disabling the agent must fail the summary, not quietly degrade it.

    A fallback would produce a working summary by the exact architecture the
    client rejected, and nothing in the output would reveal the difference.
    """
    monkeypatch.setattr(llm_client.config, "AGENT_ENABLED", False)

    with pytest.raises(llm_client.LLMResponseError) as exc:
        llm_client._summary_text_via_agent("prompt")
    assert "will not fall back" in str(exc.value)


def test_the_gate_cannot_be_satisfied_by_calling_the_tool_ourselves(monkeypatch):
    """The structural claim, stated as a test.

    Application code invoking the tool directly changes nothing about the
    agent's execution state -- which is why the gate reads state rather than a
    flag anyone can set.
    """
    policy_tool.search_underwriting_policy("late fee")  # a real, direct call

    monkeypatch.setattr(agent, "run_underwriting_agent",
                        lambda prompt: (SUMMARY_JSON, _state([_FakeAIMessage(SUMMARY_JSON)])))
    with pytest.raises(agent.RequiredToolNotCalled):
        llm_client._summary_text_via_agent("prompt")
