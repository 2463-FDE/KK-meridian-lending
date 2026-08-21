"""Every agent refusal renders as its contract, not as an internal error.

Found in review on PR #63. The agent path added five ways to refuse -- tool not
called, retrieval missed, runtime unavailable, step budget exceeded, tracing
unsafe -- and the summary route enumerated only the `LLM*Error` classes. All
five therefore fell through to the service catch-all and came back as
`500 {"detail": "internal error"}`.

That is worse than a cosmetic status bug. The refusals ARE the feature this PR
argues for: refusing an ungrounded summary is the guarantee. Rendering that
refusal as a crash means the guarantee is invisible to the officer reading the
screen and indistinguishable from a bug to whoever is on call, and a demo that
hits one looks like broken software rather than a working control.

Two more failures are covered here that the route never had at all. The agent
path does not go through `call_api`, so it silently dropped that function's
20-second timeout and its mapping to 504; and a raw provider error is on the
client's prohibited-retention list, so it must not reach the log or the
response.

No paid calls: `run_underwriting_agent` is replaced by something that raises.
"""
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from app import agent, config, llm_client, main

APP_DATA = {
    "id": 90001, "applicant_name": "Test Applicant 90001", "amount": 18000,
    "term_months": 48, "purpose": "debt consolidation", "income": 72000,
    "employment_years": 6, "status": "under_review",
}


class _Resp:
    status_code = 200

    def json(self):
        return APP_DATA

    def raise_for_status(self):
        pass


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    return TestClient(main.app, raise_server_exceptions=False)


def _raises(exc):
    def _run(prompt, agent=None):
        raise exc
    return _run


def _summary(client):
    return client.post("/applications/90001/summary",
                       headers={"X-User-Role": "underwriter"})


# --------------------------------------------------------------------------
# Each refusal, at the route.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("exc, expected", [
    (agent.RequiredToolNotCalled("the agent did not call the policy tool"), 502),
    (agent.PolicyEvidenceMissing("retrieval returned 'miss'"), 502),
    (agent.AgentStepBudgetExceeded("exceeded 12 steps"), 502),
    (agent.AgentProviderError("the model provider failed (ClientError)"), 502),
    (agent.AgentUnavailable("BEDROCK_MODEL_ID is not set"), 503),
    (agent.UnsafeTracingConfiguration("tracing is on and cannot be suppressed"), 503),
    (agent.AgentTimeout("the model did not answer within 20.0s"), 504),
])
def test_an_agent_refusal_renders_as_its_contract(client, monkeypatch, exc, expected):
    monkeypatch.setattr(llm_client.agent, "run_underwriting_agent", _raises(exc))

    resp = _summary(client)

    assert resp.status_code == expected, (
        f"{type(exc).__name__} returned {resp.status_code}: {resp.text}")
    assert resp.json()["detail"] != "internal error", (
        f"{type(exc).__name__} reached the catch-all")


def test_the_timeout_contract_survived_the_move_to_the_agent(client, monkeypatch):
    """504 specifically.

    `call_api` set `timeout=TIMEOUT_SECONDS` and mapped the provider's timeout
    to `LLMTimeoutError`, which this route renders as 504. The agent path does
    not call it, so that status had become unreachable on the summary until
    `AgentTimeout` restored it.
    """
    monkeypatch.setattr(llm_client.agent, "run_underwriting_agent",
                        _raises(agent.AgentTimeout("the model did not answer within 20.0s")))

    resp = _summary(client)

    assert resp.status_code == 504
    assert "20.0" in resp.json()["detail"]


# --------------------------------------------------------------------------
# The base class is the part that keeps working after this PR.
# --------------------------------------------------------------------------

def test_every_agent_exception_inherits_the_base(client, monkeypatch):
    """A refusal added later must not silently regress to a 500.

    Enumerating the classes in the route was what failed the first time. This
    reads the module instead, so a sixth exception defined without a base ends
    up here rather than in production.
    """
    defined = [
        obj for name, obj in vars(agent).items()
        if isinstance(obj, type) and issubclass(obj, Exception)
        and obj.__module__ == agent.__name__ and obj is not agent.AgentError
    ]

    assert defined, "no agent exceptions found -- this test would pass vacuously"
    for exc_class in defined:
        assert issubclass(exc_class, agent.AgentError), (
            f"{exc_class.__name__} does not inherit AgentError, so the route "
            f"will return 500 for it")


def test_an_unenumerated_agent_error_still_gets_a_controlled_status(client, monkeypatch):
    """The fallback, exercised rather than asserted about."""
    class _FutureRefusal(agent.AgentError):
        pass

    monkeypatch.setattr(llm_client.agent, "run_underwriting_agent",
                        _raises(_FutureRefusal("something nobody has thought of")))

    resp = _summary(client)

    assert resp.status_code == 502
    assert resp.json()["detail"] != "internal error"


# --------------------------------------------------------------------------
# Provider failures: controlled, and never quoted.
# --------------------------------------------------------------------------

class _FakeReadTimeout(Exception):
    """Stands in for botocore's ReadTimeoutError, matched by class name."""


_FakeReadTimeout.__name__ = "ReadTimeoutError"


def test_a_provider_timeout_becomes_a_timeout_refusal():
    with pytest.raises(agent.AgentTimeout):
        raise agent._as_agent_error(_FakeReadTimeout("connection to bedrock timed out"))


def test_an_unrecognised_provider_failure_becomes_a_controlled_refusal():
    with pytest.raises(agent.AgentProviderError):
        raise agent._as_agent_error(RuntimeError("something from botocore"))


def test_an_agent_error_passes_through_unchanged():
    """`_as_agent_error` must not re-wrap a refusal we already classified."""
    original = agent.PolicyEvidenceMissing("miss")

    assert agent._as_agent_error(original) is original


def test_the_raw_provider_error_never_reaches_the_log_or_the_response(
        client, monkeypatch, caplog):
    """Raw provider errors are on the client's prohibited-retention list.

    They can quote the request that caused them, and on this path the request
    is the application prompt. So the message is built from the exception CLASS
    and the original text is never formatted anywhere.
    """
    sentinel = "PROVIDER-SENTINEL-8801 rejected prompt: income 72000"

    class _ExplodingRuntime:
        def invoke(self, *a, **k):
            raise RuntimeError(sentinel)

    # The REAL run_underwriting_agent runs, so `_as_agent_error` is what is
    # under test rather than a stub standing in for it.
    monkeypatch.setattr(agent, "build_agent", lambda *a, **k: _ExplodingRuntime())

    with caplog.at_level(logging.DEBUG):
        resp = _summary(client)

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert sentinel not in logged, "the raw provider error was logged"
    assert sentinel not in resp.text, "the raw provider error reached the response"
    assert "PROVIDER-SENTINEL-8801" not in resp.text


def test_the_sentinel_test_would_catch_a_leak(caplog):
    """Guard the guard: prove the sentinel is visible when nothing suppresses it."""
    sentinel = "PROVIDER-SENTINEL-8802"

    with caplog.at_level(logging.DEBUG):
        logging.getLogger("loan-assistant").error("raw=%s", sentinel)

    assert sentinel in "\n".join(r.getMessage() for r in caplog.records)


# --------------------------------------------------------------------------
# The timeout is configured, not merely documented.
# --------------------------------------------------------------------------

def test_the_request_timeout_is_the_number_the_route_advertises():
    """20 seconds is `llm_client.TIMEOUT_SECONDS`, restated because the agent
    path stopped inheriting it -- not a new number invented here."""
    assert config.AGENT_REQUEST_TIMEOUT_SECONDS == llm_client.TIMEOUT_SECONDS


def test_the_timeout_reaches_the_bedrock_client():
    """Asserted on the constructed client, because a config object that is built
    and not passed looks identical in review."""
    pytest.importorskip("langchain_aws")
    import app.agent as agent_module

    # build_agent refuses before constructing the model unless these are set,
    # so the assertion below would never be reached.
    monkeypatch_provider = pytest.MonkeyPatch()
    monkeypatch_provider.setattr(config, "LLM_PROVIDER", "bedrock")
    monkeypatch_provider.setattr(config, "BEDROCK_MODEL_ID",
                                 "us.anthropic.claude-sonnet-4-5-20250929-v1:0")

    captured = {}

    class _FakeChatBedrock:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def bind_tools(self, tools, **kwargs):
            return self

    import langchain_aws
    original = langchain_aws.ChatBedrockConverse
    langchain_aws.ChatBedrockConverse = _FakeChatBedrock
    try:
        try:
            agent_module.build_agent()
        except Exception:
            # create_agent may reject the fake; the constructor already ran.
            pass
    finally:
        langchain_aws.ChatBedrockConverse = original
        monkeypatch_provider.undo()

    assert "config" in captured, "no botocore config was passed to the model"
    botocore_config = captured["config"]
    assert botocore_config.read_timeout == config.AGENT_REQUEST_TIMEOUT_SECONDS
    assert botocore_config.connect_timeout == config.AGENT_REQUEST_TIMEOUT_SECONDS
    assert botocore_config.retries["max_attempts"] == 3
