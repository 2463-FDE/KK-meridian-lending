"""What actually reaches LangSmith, read off the wire.

The client's prohibited-retention list is prompts, model responses, queries,
retrieved text, application/client data, identifiers, credentials, raw provider
errors and raw tool payloads. PR #63 met that by transmitting nothing at all.
This replaces zero bytes with a trace, so the burden is to show the trace is
safe by the same standard the suppression was: **measured on the wire, not
argued from the shape of Python objects**.

So every test here runs the real route against a local HTTP sink standing in for
the LangSmith endpoint, seeds a distinct sentinel into every prohibited
category, and greps the bytes that were actually posted. Inspecting
`SummaryTrace` instead would prove only that the object looks tidy; it would say
nothing about what the emitter serialises around it.

`test_the_sink_would_see_a_sentinel_without_the_allow_list` disables `_safe` and
shows the same run leaking, so a filter that silently stopped filtering cannot
leave this file green.

Four shapes are covered because they take different paths out: a successful
run, a retrieval miss, a provider/configuration failure, and a validation that
strips something. The failure paths matter most -- an error is where a payload
usually escapes, and raw provider errors are themselves on the list.
"""
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("langsmith")
# The instrumentation under test only runs when the real agent runs, and the
# real agent needs the framework. Skipped rather than stubbed in the shared
# environment: a stub here would be the very shortcut this file was corrected
# for.
pytest.importorskip("langchain")

from app import agent, config, llm_client, main, policy_tool, trace  # noqa: E402
from tests.conftest import STAFF_HEADERS

# --- one sentinel per prohibited category ----------------------------------
S_APPLICANT = "APPLICANTDATA-SENTINEL-A1"
S_NAME = "Sentinel Q. Borrower"
S_APP_ID = 987654321
S_PROMPT = "PROMPT-SENTINEL-A2"
S_MODEL_OUT = "MODELOUTPUT-SENTINEL-A3"
S_QUERY = "POLICYQUERY-SENTINEL-A4"
S_RETRIEVED = "RETRIEVEDTEXT-SENTINEL-A5"
S_PROVIDER_ERR = "PROVIDERERROR-SENTINEL-A6 credential=abcdef"
S_CREDENTIAL = "lsv2_pt_FAKE_SENTINEL_CREDENTIAL_A7"

PROHIBITED = {
    "application data": S_APPLICANT,
    "applicant name": S_NAME,
    "application identifier": str(S_APP_ID),
    "prompt": S_PROMPT,
    "model output": S_MODEL_OUT,
    "policy query": S_QUERY,
    "retrieved policy text": S_RETRIEVED,
    "raw provider error": S_PROVIDER_ERR,
    "credential": S_CREDENTIAL,
}

APP_DATA = {
    "id": S_APP_ID, "applicant_name": S_NAME, "amount": 18000,
    "term_months": 48, "purpose": f"debt consolidation {S_APPLICANT}",
    "income": 72000, "employment_years": 6, "status": "under_review",
}

SUMMARY_JSON = json.dumps({
    "loan_amount": 18000, "term_months": 48, "purpose": "debt consolidation",
    "summary": f"Adequate income. {S_MODEL_OUT}", "flags": [],
})

HIT = json.dumps({
    "status": "hit", "hit_count": 1,
    "excerpts": [{"document": "fee_schedule.md", "version": "sha256:1972040e71e5",
                  "chunk_id": "fee_schedule.md#2.0", "excerpt": S_RETRIEVED,
                  "citation": "fee_schedule.md#2.0 (sha256:1972040e71e5)"}]})
MISS = json.dumps({"status": "miss", "hit_count": 0, "excerpts": [],
                   "note": S_RETRIEVED})


class _Sink:
    def __init__(self):
        self.body = bytearray()
        captured = self.body

        class _H(BaseHTTPRequestHandler):
            def do_POST(self):
                captured.extend(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                self.send_response(202); self.end_headers(); self.wfile.write(b"{}")
            do_PATCH = do_POST
            do_PUT = do_POST
            def do_GET(self):
                self.send_response(200); self.end_headers(); self.wfile.write(b"{}")
            def log_message(self, *a):
                pass

        self._srv = HTTPServer(("127.0.0.1", 0), _H)
        self.url = "http://127.0.0.1:{}".format(self._srv.server_port)
        threading.Thread(target=self._srv.serve_forever, daemon=True).start()

    def text(self):
        try:
            import langsmith
            langsmith.client.Client().flush()
        except Exception:
            pass
        time.sleep(1.5)
        return bytes(self.body).decode("utf-8", "replace")

    def close(self):
        self._srv.shutdown(); self._srv.server_close()


class _Msg:
    def __init__(self, kind, content, name=None):
        self.type = kind
        self.content = content
        if name:
            self.name = name


def _state(*messages):
    return {"messages": list(messages)}


class _Resp:
    status_code = 200
    def json(self): return APP_DATA
    def raise_for_status(self): pass


@pytest.fixture
def sink(monkeypatch):
    s = _Sink()
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", s.url)
    monkeypatch.setenv("LANGSMITH_API_KEY", S_CREDENTIAL)
    monkeypatch.setenv("LANGSMITH_PROJECT", "privacy-regression")
    monkeypatch.setattr(config, "LANGSMITH_API_KEY", S_CREDENTIAL)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    yield s
    s.close()


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


def _real_agent_over_a_fake_model(monkeypatch, tool_payload=HIT, answer=SUMMARY_JSON):
    """Fake ONLY the external model. Everything else is the real code.

    Corrected in review, and the correction matters more than the code it
    replaced. These tests used to monkeypatch `run_underwriting_agent` -- which
    is the function that records the `model` and `agent_run` stages. Stubbing it
    meant the instrumentation under test never executed, so the wire assertions
    were made about a trace those stages had never contributed to, and the
    stages could have been emitting anything at all.

    Now the route runs `summarize_application`, the real
    `run_underwriting_agent`, the real LangChain graph, the real tool node, the
    real evidence gate, the real validators and the real emitter. Only the
    Bedrock model is fake, because that is the one thing that would cost money
    and the one thing whose output the test needs to control.
    """
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult

    turns = {"n": 0}

    class _FakeModel(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

        def _generate(self, messages, stop=None, run_manager=None, **kwargs):
            turns["n"] += 1
            if turns["n"] == 1:
                msg = AIMessage(content="", tool_calls=[{
                    "name": policy_tool.TOOL_NAME,
                    "args": {"query": S_QUERY}, "id": "call-1"}])
            else:
                msg = AIMessage(content=answer)
            return ChatResult(generations=[ChatGeneration(message=msg)])

    from langchain_core.tools import StructuredTool

    def _search(query: str, top_k: int = 3) -> str:
        return tool_payload

    tool = StructuredTool.from_function(
        func=_search, name=policy_tool.TOOL_NAME,
        description="Search the client's underwriting policy documents.")

    runtime = create_agent(model=_FakeModel(messages=iter([])), tools=[tool],
                           system_prompt="system contract")
    monkeypatch.setattr(agent, "build_agent", lambda *a, **k: runtime)
    return runtime


def _run(monkeypatch, runner):
    """Legacy seam, kept only where the test is about the ROUTE's mapping of an
    already-classified refusal rather than about instrumentation."""
    monkeypatch.setattr(llm_client.agent, "run_underwriting_agent", runner)


def _post(client):
    return client.post(f"/applications/{S_APP_ID}/summary",
                       headers=STAFF_HEADERS)


def _span_names(blob):
    """Span names actually present on the wire.

    Substring-matching the blob for a stage name is not enough and a mutation
    proved it: deleting the `model` stage entirely left the test green, because
    the word "model" also appears in `model_turns` and in the SDK's own
    metadata. Names are matched as names.
    """
    return set(re.findall(r'"name"\s*:\s*"([a-z_]+)"', blob))


def _leaks(blob):
    return {name: s for name, s in PROHIBITED.items() if s in blob}


# --------------------------------------------------------------------------
# The four shapes.
# --------------------------------------------------------------------------

def test_a_successful_run_leaks_nothing(sink, client, monkeypatch):
    _real_agent_over_a_fake_model(monkeypatch)

    resp = _post(client)
    assert resp.status_code == 200, resp.text

    blob = sink.text()
    assert blob, "nothing was transmitted -- this test would pass vacuously"
    assert not _leaks(blob), f"prohibited content on the wire: {_leaks(blob)}"


def test_a_retrieval_miss_leaks_nothing(sink, client, monkeypatch):
    _real_agent_over_a_fake_model(monkeypatch, tool_payload=MISS)

    resp = _post(client)
    assert resp.status_code == 502

    blob = sink.text()
    assert blob
    assert not _leaks(blob), f"prohibited content on the wire: {_leaks(blob)}"
    assert "PolicyEvidenceMissing" in blob, "the refusal category was not recorded"


def test_a_provider_failure_leaks_nothing_including_its_own_text(sink, client, monkeypatch):
    """The raw provider error is itself on the prohibited list, and an error
    path is where a payload most often escapes."""
    class _Exploding:
        def invoke(self, *a, **k):
            raise RuntimeError(S_PROVIDER_ERR)

    monkeypatch.setattr(agent, "build_agent", lambda *a, **k: _Exploding())

    resp = _post(client)
    assert resp.status_code == 502

    blob = sink.text()
    assert blob
    assert not _leaks(blob), f"prohibited content on the wire: {_leaks(blob)}"
    assert "AgentProviderError" in blob


def test_a_configuration_failure_leaks_nothing(sink, client, monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "bedrock")
    monkeypatch.setattr(config, "BEDROCK_MODEL_ID", "")

    resp = _post(client)
    assert resp.status_code == 503

    blob = sink.text()
    assert blob
    assert not _leaks(blob)
    assert "AgentUnavailable" in blob


def test_a_validation_strip_is_recorded_without_the_text_it_removed(sink, client, monkeypatch):
    """A guardrail firing is exactly the thing an operator needs to see, and
    exactly the thing that must not carry the sentence it deleted."""
    dti = json.dumps({
        "loan_amount": 18000, "term_months": 48, "purpose": "debt consolidation",
        # Two real sentences. One sentence would be stripped entirely and the
        # summary refused -- correct behaviour, but a different test than this.
        "summary": (f"Adequate income for the requested amount. {S_MODEL_OUT}. "
                    f"The applicant's debt-to-income ratio is 22%."),
        "flags": [],
    })
    _run(monkeypatch, lambda prompt: (dti, _state(
        _Msg("tool", HIT, policy_tool.TOOL_NAME), _Msg("ai", dti))))

    resp = _post(client)
    assert resp.status_code == 200

    blob = sink.text()
    assert not _leaks(blob), f"prohibited content on the wire: {_leaks(blob)}"
    assert "dti_claim" in blob, "the triggered validator was not recorded"
    assert "debt-to-income ratio is 22" not in blob


@pytest.mark.parametrize("upstream_status, expected_class", [
    (404, "application_not_found"),
    (403, "forbidden"),
])
def test_a_failure_before_the_agent_still_records_an_outcome(
        sink, client, monkeypatch, upstream_status, expected_class):
    """A request that never reaches the agent is still a request that was
    answered.

    Found in review. These exits raise `HTTPException` from inside the route
    before the block that records the outcome, so the trace stopped after
    `request` -- which reads as a request that vanished, the exact ambiguity a
    trace exists to remove.
    """
    class _Upstream:
        status_code = upstream_status
        def json(self): return {}
        def raise_for_status(self): pass

    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Upstream())

    resp = _post(client)
    assert resp.status_code == upstream_status

    blob = sink.text()
    names = _span_names(blob)
    assert "outcome" in names, (
        f"a {upstream_status} emitted only {sorted(names)}")
    assert expected_class in blob
    assert not _leaks(blob), f"prohibited content on the wire: {_leaks(blob)}"


# --------------------------------------------------------------------------
# The categorical events the client asked to SEE.
# --------------------------------------------------------------------------

def test_every_required_stage_reaches_the_endpoint(sink, client, monkeypatch):
    _real_agent_over_a_fake_model(monkeypatch)

    _post(client)
    blob = sink.text()

    # agent_run and model included deliberately: both were declared in
    # trace.STAGES and neither was required here, so `agent_run` was emitting
    # nothing at all and nobody noticed.
    names = _span_names(blob)
    for stage in ("request", "agent_run", "model", "policy_retrieval",
                  "validation", "outcome"):
        assert stage in names, (
            f"stage {stage!r} never reached the endpoint; saw {sorted(names)}")
    for categorical in ("underwriting_summary", "privacy_safe_categorical",
                        "search_underwriting_policy", "hit",
                        "summary_returned", "underwriter"):
        assert categorical in blob, f"{categorical!r} missing from the trace"


def test_policy_provenance_travels_but_policy_text_does_not(sink, client, monkeypatch):
    """Document, version and citation identify the client's own published
    policy, not an applicant. The excerpt beside them does not travel."""
    _real_agent_over_a_fake_model(monkeypatch)

    _post(client)
    blob = sink.text()

    assert "fee_schedule.md" in blob
    assert "sha256:1972040e71e5" in blob
    assert "fee_schedule.md#2.0" in blob
    assert S_RETRIEVED not in blob


# --------------------------------------------------------------------------
# Guard the guard.
# --------------------------------------------------------------------------

def test_the_sink_would_see_a_sentinel_without_the_allow_list(sink, client, monkeypatch):
    """Without `_safe`, the same run leaks.

    This is what makes the tests above evidence rather than decoration: it
    proves the sink observes sentinels on this transport, and that the
    allow-list is the thing preventing it -- not some accident of the emitter.
    """
    monkeypatch.setattr(trace, "_safe", lambda fields: dict(fields or {}))
    _real_agent_over_a_fake_model(monkeypatch)

    # Feed a prohibited value through a stage the disabled filter will now pass.
    original = llm_client._policy_provenance

    def _leaky(state):
        documents, versions, citations = original(state)
        return documents + [S_RETRIEVED], versions, citations

    monkeypatch.setattr(llm_client, "_policy_provenance", _leaky)

    _post(client)
    blob = sink.text()

    assert S_RETRIEVED in blob, (
        "the sink saw nothing even with the allow-list disabled, so the "
        "privacy assertions above prove nothing")


def test_the_allow_list_rejects_unknown_keys_and_unvocabularied_values():
    """Fails closed in both directions, asserted directly on the filter."""
    admitted = trace._safe({
        "stage": "request",                    # known key, known value
        "evidence_status": "hit",
        "surprise_field": "anything",          # unknown key
        "status": "definitely-not-a-status",   # known key, unknown value
        "documents": ["fee_schedule.md", S_RETRIEVED],   # one shaped, one not
        "citations": [S_APPLICANT],
        "tool_calls": 3,
        "http_status": 99999,                  # out of range
    })

    assert admitted["stage"] == "request"
    assert admitted["evidence_status"] == "hit"
    assert admitted["tool_calls"] == 3
    assert "surprise_field" not in admitted
    assert "status" not in admitted
    assert "http_status" not in admitted
    assert admitted["documents"] == ["fee_schedule.md"]
    assert "citations" not in admitted


# The filter has three layers, and mutation testing showed they were masking
# one another: disabling any single layer left every test green because another
# layer happened to catch the same value. Layers that can only be verified
# together are not defence in depth, they are one layer with two names. Each
# test below is shaped so exactly one layer is what makes it pass.

def test_layer_one_the_key_allow_list_stands_on_its_own(monkeypatch):
    """A bool short-circuits the value checks, so only ALLOWED_FIELDS can
    reject it. That isolates the key filter from the value filters."""
    assert trace._safe({"stage": "request", "surprise_field": True}) == {"stage": "request"}


def test_layer_two_the_value_checks_stand_on_their_own(monkeypatch):
    """An ALLOWED key carrying a value no vocabulary or shape admits."""
    monkeypatch.setattr(trace, "ALLOWED_FIELDS", frozenset(trace.ALLOWED_FIELDS))

    assert trace._safe({"evidence_status": "SENTINEL-not-a-status"}) == {}
    assert trace._safe({"documents": ["../../etc/passwd"]}) == {}
    assert trace._safe({"citations": ["a paragraph of retrieved policy text"]}) == {}


def test_layer_three_an_allowed_key_with_no_vocabulary_is_still_refused():
    """`schema_version` is allowed as a key and has neither a vocabulary nor a
    shape. It must still be refused, because "allowed key" is not the same as
    "reviewed value" -- that final `return None` is the only thing standing
    between a future allowed key and free text."""
    assert trace._safe({"schema_version": "SENTINEL-free-text"}) == {}


def test_the_payload_refilters_what_was_written_around_the_recorder():
    """`payload()` filters again on the way out.

    Redundant against `record`/`annotate`, and deliberately so: those are not
    the only way a field can end up on a span, and the guarantee has to hold at
    the boundary that actually transmits rather than at the one that is
    conventionally used.
    """
    t = trace.SummaryTrace()
    t.record("request", service="loan-assistant")
    t.spans[-1].fields["leaked"] = "SENTINEL-written-directly"
    t.spans[-1].fields["evidence_status"] = "SENTINEL-not-a-status"

    metadata = t.payload()["spans"][-1]["metadata"]

    assert "leaked" not in metadata
    assert "evidence_status" not in metadata
    assert metadata["service"] == "loan-assistant"


def test_the_excerpt_field_is_never_read_into_provenance():
    """The provenance reader sees the same payload the model saw.

    Naming the three identifier fields is what keeps the excerpt out, so the
    reader is asserted directly rather than only through the filter that would
    also have caught it.
    """
    import inspect

    source = inspect.getsource(llm_client._policy_provenance)

    assert '"excerpt"' not in source, (
        "the provenance reader references the excerpt field")
    for field in ('"document"', '"version"', '"citation"'):
        assert field in source


def test_the_credential_is_never_traced_even_though_the_emitter_holds_it(sink, client, monkeypatch):
    """The API key is on the prohibited list and is in this process's
    environment, so it is the easiest thing in the world to emit by accident."""
    _real_agent_over_a_fake_model(monkeypatch)

    _post(client)

    body = sink.text()
    assert S_CREDENTIAL not in body


def test_no_provider_credential_from_the_environment_reaches_the_wire(
        sink, client, monkeypatch):
    """The LangSmith SDK attaches its own runtime metadata to every run.

    Observed on the wire: `LANGSMITH_ENDPOINT`, `LANGSMITH_PROJECT`,
    `revision_id`, SDK and platform versions. That is the emitter's own
    behaviour, not this module's, so what it collects has to be checked rather
    than assumed -- a future SDK that widened its environment capture would
    ship provider credentials without a line of this repository changing.
    """
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "AWSBEARER-SENTINEL-A8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-SENTINEL-A9")
    monkeypatch.setenv("INTERNAL_SERVICE_TOKEN", "INTERNALTOKEN-SENTINEL-A10")
    _real_agent_over_a_fake_model(monkeypatch)

    _post(client)
    blob = sink.text()

    for name, sentinel in (("bedrock bearer token", "AWSBEARER-SENTINEL-A8"),
                           ("anthropic key", "sk-ant-SENTINEL-A9"),
                           ("internal service token", "INTERNALTOKEN-SENTINEL-A10")):
        assert sentinel not in blob, f"{name} reached the trace endpoint"


def test_nothing_is_emitted_when_tracing_is_off(sink, client, monkeypatch):
    """The zero-byte baseline is still reachable, and is still the default."""
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    monkeypatch.delenv("LANGCHAIN_TRACING_V2", raising=False)
    monkeypatch.delenv("LANGCHAIN_TRACING", raising=False)
    _real_agent_over_a_fake_model(monkeypatch)

    assert _post(client).status_code == 200
    assert sink.text() == ""


def test_the_framework_auto_trace_stays_suppressed(sink, client, monkeypatch):
    """PR #63's suppression is the safety baseline and is NOT lifted here.

    The privacy-safe trace is emitted alongside it, explicitly. If the
    suppression were removed so the framework could trace instead, the ~31KB
    of prompt and retrieved text would return.
    """
    import inspect

    source = inspect.getsource(agent.run_underwriting_agent)

    assert "with suppressed_tracing():" in source, (
        "the framework's own tracing is no longer suppressed around invoke")
