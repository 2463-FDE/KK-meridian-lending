"""Model responses are not retained in logs -- redacted or otherwise.

The client's requirement is stricter than this service's existing PII rule.
`redact_str` masks card, SSN and CVV shapes; it leaves the rest of the model's
text intact. Two log lines did exactly that on the failure path:

    log.error("llm_client parse error response=%s", safe_raw)
    log.error("policy_chat parse error response=%s", safe_raw)

A redacted response is still a retained response. Parse failure is also the
worst moment for it, because the text that failed to parse is the text most
likely to be something unexpected.

These tests assert the property at two levels: no call site formats model text
into a log, and running the failure path with a sentinel response leaves the
sentinel out of the captured output. The second is what actually matters -- the
first would pass on a cleverly renamed variable.
"""
import json
import logging
import pathlib
import re

import pytest

from app import agent, llm_client, policy_chat, policy_tool

APP = pathlib.Path(__file__).resolve().parents[1] / "app"

SENTINEL_RESPONSE = "MODEL-SAID-THIS-ZZ42"
SENTINEL_PROMPT = "PROMPT-SAID-THIS-ZZ43"


def _logged(caplog):
    return "\n".join(r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# Behavioural: run the failure paths and look at what was captured.
# --------------------------------------------------------------------------

def test_a_summary_parse_failure_logs_no_model_text(monkeypatch, caplog):
    class _Tool:
        type = "tool"
        name = policy_tool.TOOL_NAME
        content = "{}"

    monkeypatch.setattr(
        agent, "run_underwriting_agent",
        lambda prompt: (SENTINEL_RESPONSE, {"messages": [_Tool()]}))

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(llm_client.LLMResponseError):
            llm_client._parse_summary_text(SENTINEL_RESPONSE)

    logged = _logged(caplog)
    assert SENTINEL_RESPONSE not in logged
    assert "stage=summary_parse" in logged, "the stage should still be identifiable"


def test_a_policy_chat_parse_failure_logs_no_model_text(monkeypatch, caplog):
    monkeypatch.setattr(policy_chat.llm_client, "make_client", lambda: object())
    monkeypatch.setattr(policy_chat.llm_client, "call_api",
                        lambda *a, **k: SENTINEL_RESPONSE)

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(policy_chat.PolicyChatResponseError):
            policy_chat.answer_policy_question("what is the late fee?")

    logged = _logged(caplog)
    assert SENTINEL_RESPONSE not in logged
    assert "stage=answer_parse" in logged


def test_the_exception_message_carries_no_model_text(monkeypatch):
    """Exceptions are logged by the framework at the edge, so a message
    carrying the response reintroduces the leak one layer up."""
    monkeypatch.setattr(policy_chat.llm_client, "make_client", lambda: object())
    monkeypatch.setattr(policy_chat.llm_client, "call_api",
                        lambda *a, **k: SENTINEL_RESPONSE)

    with pytest.raises(policy_chat.PolicyChatResponseError) as exc:
        policy_chat.answer_policy_question("what is the late fee?")
    assert SENTINEL_RESPONSE not in str(exc.value)


def test_a_successful_summary_logs_no_prompt_or_response(monkeypatch, caplog):
    """The happy path is where volume lives, so it is where a leak would be
    most widely written."""
    class _Tool:
        type = "tool"
        name = policy_tool.TOOL_NAME
        content = "{}"

    payload = json.dumps({
        "loan_amount": 1000, "term_months": 12, "purpose": "debt consolidation",
        "summary": f"{SENTINEL_RESPONSE} adequate income.", "flags": [],
    })
    monkeypatch.setattr(agent, "run_underwriting_agent",
                        lambda prompt: (payload, {"messages": [_Tool()]}))

    with caplog.at_level(logging.DEBUG):
        llm_client._summary_text_via_agent(SENTINEL_PROMPT)

    logged = _logged(caplog)
    assert SENTINEL_RESPONSE not in logged
    assert SENTINEL_PROMPT not in logged


# --------------------------------------------------------------------------
# Structural: the call sites themselves.
# --------------------------------------------------------------------------

#: Names that hold model text at the point of logging. Matched as a log
#: argument, not anywhere in the file, so prose about them stays legal.
_CONTENT_ARGS = re.compile(
    r"log\.\w+\([^)]*\b(raw|safe_raw|response|answer|completion|prompt|text|content)\b[^)]*\)",
    re.S)


@pytest.mark.parametrize("module", sorted(p.name for p in APP.glob("*.py")))
def test_no_log_call_formats_model_content(module):
    source = (APP / module).read_text(encoding="utf-8")

    offenders = []
    for match in _CONTENT_ARGS.finditer(source):
        call = match.group(0)
        # `response=%s` with a categorical value is fine; the argument list is
        # what matters. A bare format placeholder plus one of these names as an
        # ARGUMENT is the shape being forbidden.
        if re.search(r",\s*(safe_raw|raw|prompt|completion)\b", call):
            offenders.append(call.replace("\n", " ")[:120])

    assert not offenders, (
        f"{module} formats model content into a log call: {offenders}"
    )


def test_the_structural_check_can_actually_fail():
    """Guard the guard -- a regex matching nothing would pass on any file."""
    sample = 'log.error("parse error response=%s", safe_raw)'

    assert _CONTENT_ARGS.search(sample) is not None
    assert re.search(r",\s*(safe_raw|raw|prompt|completion)\b", sample)
