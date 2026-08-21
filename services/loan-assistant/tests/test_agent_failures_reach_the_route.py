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
import json
import logging
import sys

import httpx
import pytest
from fastapi.testclient import TestClient

from app import agent, config, llm_client, main, policy_tool

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
    # total_max_attempts, not max_attempts -- see test_f8_* for why the two
    # differ by one and which of them means what this code intends.
    assert botocore_config.retries["total_max_attempts"] == 3

# --------------------------------------------------------------------------
# A1 re-proved through the REAL code paths.
#
# The cases above inject the exception, which proves the route's mapping. These
# five provoke each refusal the way production would -- a runtime that returns a
# state with no tool message, a retrieval that misses, a graph that recurses, a
# model id that is unset, a suppressor that cannot be imported -- so the
# assertion covers the whole chain from the FastAPI route down, not just the
# except clause. Requested in review; the earlier version could have passed
# while the code that RAISES had drifted.
#
# Sentinels stand in for content that must never reach the officer's screen or
# the log: the applicant's own data, the model's text, the tool's output.
# --------------------------------------------------------------------------

PROMPT_SENTINEL = "72000"                      # applicant income, from APP_DATA
MODEL_SENTINEL = "MODEL-TEXT-SENTINEL-4401"    # what the model wrote
TOOL_SENTINEL = "TOOL-OUTPUT-SENTINEL-4402"    # what retrieval returned

_MISS_PAYLOAD = json.dumps({"status": "miss", "hit_count": 0, "excerpts": [],
                            "note": TOOL_SENTINEL})
_HIT_PAYLOAD = json.dumps({
    "status": "hit", "hit_count": 1,
    "excerpts": [{"document": "fee_schedule.md", "version": "sha256:abc",
                  "chunk_id": "fee_schedule.md#1.0", "excerpt": TOOL_SENTINEL,
                  "citation": "c"}]})


class _ToolMsg:
    type = "tool"

    def __init__(self, content, name=None):
        self.name = name or policy_tool.TOOL_NAME
        self.content = content


class _AIMsg:
    type = "ai"

    def __init__(self, content):
        self.content = content


class _Runtime:
    """A fake LangChain runtime: returns a state, or raises like one would."""

    def __init__(self, state=None, raises=None):
        self._state = state
        self._raises = raises

    def invoke(self, payload, config=None):
        if self._raises is not None:
            raise self._raises
        return self._state


class _GraphRecursionError(Exception):
    """`run_underwriting_agent` matches this by class name, as LangGraph's is
    not importable without the framework."""


_GraphRecursionError.__name__ = "GraphRecursionError"


def _real_path(monkeypatch, runtime):
    """Wire the route to a real agent run over a fake runtime.

    `build_agent` is the only thing replaced, so `run_underwriting_agent`, the
    tool gate, the evidence gate and the error classification all execute.
    """
    monkeypatch.setattr(agent, "build_agent", lambda *a, **k: runtime)


A1_CASES = {
    "1. required policy tool skipped": (
        502, "RequiredToolNotCalled",
        lambda mp: _real_path(mp, _Runtime(state={"messages": [_AIMsg(MODEL_SENTINEL)]}))),

    "2. tool ran but retrieval missed": (
        502, "PolicyEvidenceMissing",
        lambda mp: _real_path(mp, _Runtime(state={"messages": [
            _ToolMsg(_MISS_PAYLOAD), _AIMsg(MODEL_SENTINEL)]}))),

    "3. step budget exceeded": (
        502, "AgentStepBudgetExceeded",
        lambda mp: _real_path(mp, _Runtime(raises=_GraphRecursionError("recursion limit")))),

    "4. agent unavailable (no Bedrock model id)": (
        503, "AgentUnavailable",
        lambda mp: (mp.setattr(config, "LLM_PROVIDER", "bedrock"),
                    mp.setattr(config, "BEDROCK_MODEL_ID", ""))),

    "5. tracing cannot be safely suppressed": (
        503, "UnsafeTracingConfiguration",
        lambda mp: (mp.setenv("LANGSMITH_TRACING", "true"),
                    # Blocks `from langsmith.run_helpers import tracing_context`
                    # with an ImportError, which is the only way the suppressor
                    # can be unavailable.
                    mp.setitem(sys.modules, "langsmith.run_helpers", None),
                    _real_path(mp, _Runtime(state={"messages": [
                        _ToolMsg(_HIT_PAYLOAD), _AIMsg(MODEL_SENTINEL)]})))),
}


@pytest.mark.parametrize("label", list(A1_CASES))
def test_a1_each_refusal_reproduced_through_the_route(client, monkeypatch, caplog, label):
    expected_status, expected_class, arrange = A1_CASES[label]
    arrange(monkeypatch)

    with caplog.at_level(logging.DEBUG):
        resp = _summary(client)

    body = resp.text
    detail = resp.json().get("detail", "")
    logged = "\n".join(r.getMessage() for r in caplog.records)

    # 1. not a generic internal error
    assert resp.status_code != 500, f"{label}: generic 500 -- {body}"
    assert detail != "internal error", f"{label}: reached the catch-all"

    # 2. maps into the controlled contract
    assert resp.status_code == expected_status, (
        f"{label}: expected {expected_status}, got {resp.status_code}: {body}")

    # 3. the detail says something a reader can act on
    assert isinstance(detail, str) and len(detail) > 20, (
        f"{label}: detail is not useful: {detail!r}")

    # 4. no prompt, model or tool content anywhere in the response
    for name, sentinel in (("applicant data", PROMPT_SENTINEL),
                           ("model text", MODEL_SENTINEL),
                           ("tool output", TOOL_SENTINEL)):
        assert sentinel not in body, f"{label}: {name} leaked into the response"

    # 5. logs carry the category, not the content
    for name, sentinel in (("model text", MODEL_SENTINEL),
                           ("tool output", TOOL_SENTINEL)):
        assert sentinel not in logged, f"{label}: {name} was logged"
    # 6. the log names WHICH refusal -- strict, because an `or` fallback here
    #    would let a case pass on the wrong refusal and prove nothing about the
    #    path it claims to exercise.
    assert expected_class in logged, (
        f"{label}: expected the log to name {expected_class}; got: {logged[-300:]!r}")


def test_a1_the_five_refusals_stay_distinguishable_internally(monkeypatch):
    """The HTTP layer collapses these onto three statuses. The domain must not.

    PR B records which refusal happened as trace metadata, so flattening them
    into one exception to make the route simpler would destroy the categories
    that work depends on. Two share a status and are still different classes.
    """
    classes = {
        agent.RequiredToolNotCalled, agent.PolicyEvidenceMissing,
        agent.AgentStepBudgetExceeded, agent.AgentUnavailable,
        agent.UnsafeTracingConfiguration,
    }

    assert len(classes) == 5, "refusal classes were merged"
    for exc_class in classes:
        assert issubclass(exc_class, agent.AgentError)
        others = classes - {exc_class}
        assert not any(issubclass(exc_class, other) for other in others), (
            f"{exc_class.__name__} is a subclass of another refusal, so the two "
            f"cannot be told apart by except-clause or by isinstance")

    # 502 is shared by three of them; the status is not the category.
    assert agent.RequiredToolNotCalled is not agent.PolicyEvidenceMissing

# --------------------------------------------------------------------------
# F8 / F9 / F10 -- the three narrow points, each asserted against what the
# installed libraries actually do rather than against what the names suggest.
# --------------------------------------------------------------------------

def _model_kwargs():
    """Construct the agent over a fake model class and return the kwargs it got."""
    pytest.importorskip("langchain_aws")
    import langchain_aws

    captured = {}

    class _FakeChatBedrock:
        def __init__(self, **kwargs):
            captured.update(kwargs)

        def bind_tools(self, tools, **kwargs):
            return self

    mp = pytest.MonkeyPatch()
    mp.setattr(config, "LLM_PROVIDER", "bedrock")
    mp.setattr(config, "BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0")
    original = langchain_aws.ChatBedrockConverse
    langchain_aws.ChatBedrockConverse = _FakeChatBedrock
    try:
        try:
            agent.build_agent()
        except Exception:
            pass  # create_agent may reject the fake; the constructor already ran
    finally:
        langchain_aws.ChatBedrockConverse = original
        mp.undo()
    return captured


# --- F8 -------------------------------------------------------------------

def test_f8_the_attempt_limit_is_three_TOTAL_not_three_retries():
    """`max_attempts` and `total_max_attempts` differ by one, and the name that
    reads correctly is the wrong one.

    Measured on the installed botocore: `retries={"max_attempts": 3}` resolves
    to `total_max_attempts=4` -- three retries AFTER the initial request. The
    intent is three attempts in total, so the key that means that is used.
    """
    retries = _model_kwargs()["config"].retries

    assert retries.get("total_max_attempts") == agent.AGENT_TOTAL_PROVIDER_ATTEMPTS == 3
    assert "max_attempts" not in retries, (
        "max_attempts means retries-after-the-first; this would be 4 total")


def test_f8_botocore_really_does_treat_the_two_keys_differently():
    """Guard the guard.

    If a future botocore made `max_attempts` mean total, the test above would
    be enforcing a distinction that no longer exists. This asserts the
    distinction itself, so the reason for the choice is checked and not just
    the choice.
    """
    pytest.importorskip("botocore")
    from botocore.config import Config
    from botocore.session import get_session

    def _resolved(retries):
        client = get_session().create_client(
            "bedrock-runtime", region_name="us-east-1",
            aws_access_key_id="not-a-real-key", aws_secret_access_key="not-a-real-secret",
            config=Config(retries=retries))
        return client.meta.config.retries

    assert _resolved({"max_attempts": 3, "mode": "standard"})["total_max_attempts"] == 4, (
        "botocore no longer adds the initial request to max_attempts")
    assert _resolved({"total_max_attempts": 3, "mode": "standard"})["total_max_attempts"] == 3


def test_f8_the_worst_case_provider_attempt_exposure_is_bounded():
    """The number quoted in the PR body, asserted rather than asserted-about.

    AGENT_MAX_STEPS permits at most half its value in model invocations, since
    the graph alternates model and tool nodes. Each invocation may make up to
    AGENT_TOTAL_PROVIDER_ATTEMPTS provider attempts.
    """
    max_model_invocations = config.AGENT_MAX_STEPS // 2
    exposure = max_model_invocations * agent.AGENT_TOTAL_PROVIDER_ATTEMPTS

    assert max_model_invocations == 6
    assert exposure == 18, (
        f"worst-case provider attempts per summary changed to {exposure}; "
        f"update the PR body and the comment in build_agent")


def test_f8_the_model_invocation_bound_is_real_not_arithmetic():
    """Drive a real graph to its recursion limit and count model invocations.

    `AGENT_MAX_STEPS // 2` is a claim about how LangGraph counts steps. This
    checks it against the framework instead of trusting the division.
    """
    pytest.importorskip("langchain")
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.tools import StructuredTool

    calls = {"n": 0}

    class _AlwaysCallsTheTool(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            calls["n"] += 1
            message = AIMessage(content="", tool_calls=[{
                "name": policy_tool.TOOL_NAME, "args": {"query": "fee"},
                "id": f"call-{calls['n']}"}])
            return ChatResult(generations=[ChatGeneration(message=message)])

    tool = StructuredTool.from_function(
        func=policy_tool.search_underwriting_policy,
        name=policy_tool.TOOL_NAME, description="search policy")
    runtime = create_agent(model=_AlwaysCallsTheTool(messages=iter([])),
                           tools=[tool], system_prompt="x")

    with pytest.raises(agent.AgentStepBudgetExceeded):
        agent.run_underwriting_agent("prompt", runtime)

    assert calls["n"] == config.AGENT_MAX_STEPS // 2 == 6, (
        f"the step budget allowed {calls['n']} model invocations, not 6")


# --- F9 -------------------------------------------------------------------

def test_f9_the_timeout_is_described_as_per_attempt_not_as_a_deadline():
    """CODE == DOCUMENTED CONTRACT == TEST.

    `connect_timeout`/`read_timeout` bound one connect and one read on one HTTP
    attempt. They do not bound an attempt sequence, a model invocation or the
    run, and botocore exposes no knob that does. The wording must not imply
    otherwise -- the first version did, having inherited the framing from
    `call_api`, where a `@retry(stop_after_attempt(3))` decorator meant its own
    "20s" was never a wall either.
    """
    detail = str(agent._as_agent_error(_FakeReadTimeout("timed out")))

    assert "per-attempt" in detail or "transport" in detail, (
        f"the timeout message does not say what it bounds: {detail!r}")
    for overclaim in ("did not answer within", "deadline", "total"):
        assert overclaim not in detail, (
            f"the timeout message implies a wall it does not enforce: {detail!r}")


def test_f9_the_transport_timeout_reaches_the_client_on_both_legs():
    botocore_config = _model_kwargs()["config"]

    assert botocore_config.connect_timeout == config.AGENT_REQUEST_TIMEOUT_SECONDS
    assert botocore_config.read_timeout == config.AGENT_REQUEST_TIMEOUT_SECONDS


# --- F10 ------------------------------------------------------------------

def test_f10_an_unexpected_construction_failure_is_controlled(client, monkeypatch, caplog):
    """The provider SDK's own constructor raising is not a configuration
    refusal, and it used to escape the classification boundary entirely.

    Reproduced by making the real `ChatBedrockConverse` construction path raise
    -- not by raising `AgentUnavailable` in the route, which would prove only
    that the route maps a class it is already given.
    """
    pytest.importorskip("langchain_aws")
    import langchain_aws

    sentinel = "PROVIDER-CONSTRUCTOR-SENTINEL rejected config / secret-looking-data"

    class _ExplodingModel:
        def __init__(self, **kwargs):
            raise RuntimeError(sentinel)

    monkeypatch.setattr(langchain_aws, "ChatBedrockConverse", _ExplodingModel)
    monkeypatch.setattr(config, "LLM_PROVIDER", "bedrock")
    monkeypatch.setattr(config, "BEDROCK_MODEL_ID",
                        "us.anthropic.claude-sonnet-4-5-20250929-v1:0")

    with caplog.at_level(logging.DEBUG):
        resp = _summary(client)

    logged = "\n".join(r.getMessage() for r in caplog.records)

    assert resp.status_code == 503, f"expected 503, got {resp.status_code}: {resp.text}"
    assert resp.json()["detail"] != "internal error"
    assert sentinel not in resp.text, "the constructor error reached the response"
    assert sentinel not in logged, "the constructor error was logged raw"
    assert "RuntimeError" in resp.json()["detail"], (
        "the refusal should still name the failure category")
    assert "stage=agent_construct" in logged, (
        "the log should distinguish construction from invocation")


def test_f10_a_construction_failure_keeps_its_chained_cause():
    """Raw text stays reachable for a debugger, never formatted into output."""
    original = RuntimeError("CAUSE-SENTINEL-7712")

    classified = agent._as_agent_error(original, stage="construct")

    assert isinstance(classified, agent.AgentUnavailable)
    assert "CAUSE-SENTINEL-7712" not in str(classified)


def test_f10_configuration_refusals_still_map_to_503_unchanged(client, monkeypatch):
    """The known construction refusals must not be reclassified by the fix."""
    # Without the framework installed, build_agent refuses on the missing
    # dependency FIRST -- still a 503, but a different refusal than the one
    # this test is about. Gated rather than loosened, so the assertion stays
    # specific to the model-id case.
    pytest.importorskip("langchain_aws")
    monkeypatch.setattr(config, "LLM_PROVIDER", "bedrock")
    monkeypatch.setattr(config, "BEDROCK_MODEL_ID", "")

    resp = _summary(client)

    assert resp.status_code == 503
    assert "BEDROCK_MODEL_ID" in resp.json()["detail"]


def test_f10_construction_and_invocation_stay_distinguishable():
    """Flattening both onto one class would lose a category PR B needs."""
    from_construct = agent._as_agent_error(RuntimeError("x"), stage="construct")
    from_invoke = agent._as_agent_error(RuntimeError("x"), stage="invoke")

    assert isinstance(from_construct, agent.AgentUnavailable)
    assert isinstance(from_invoke, agent.AgentProviderError)
    assert type(from_construct) is not type(from_invoke)
