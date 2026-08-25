"""One trace, from the authenticated entry point to the outcome, read off the wire.

`test_trace_is_privacy_safe.py` proved what this service transmits. It could not
prove where the trace STARTS, because it starts in another service: the client
asked for "UI/gateway entry through ... final outcome", and this module's own
docstring used to name the missing hop as a remaining gap. The gateway now opens
a `gateway_entry` run after it authorises the caller and forwards the context on
LangSmith's two propagation headers.

So this file asserts the join, and asserts it the same way the privacy file
asserts safety -- **on the bytes actually posted**, driving the real route, the
real `run_underwriting_agent`, the real LangChain graph, the real tool node, the
real evidence gate, the real validators and the real emitter. Only two things are
fake: the Bedrock model, because it costs money and its output has to be
controlled, and the LangSmith endpoint, because that is the thing being read.

Three claims, each separately falsifiable:

  1. **The tree.** `gateway_entry` is the root, this service's run hangs beneath
     it, and the six stages hang beneath that -- one trace, not two.
  2. **The stages.** Exactly the declared set arrives; a deleted or renamed stage
     fails rather than passing quietly.
  3. **The scrub.** `baggage` carries metadata and the SDK MERGES a parent's
     metadata into its children, so an inbound context is a route into these
     spans from outside the allow-list. `_inbound_parent` keeps identity only,
     and the guard-the-guard test at the bottom shows the leak reappearing when
     that scrub is removed.

The gateway's own module is imported by file path rather than copied. It depends
on nothing but the standard library and `langsmith`, so it loads cleanly here,
and importing the real minting code is the difference between testing the
contract and testing a restatement of it.
"""
import importlib.util
import json
import pathlib
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("langsmith")
# The join happens in the emitter, which only runs when the real agent runs, and
# the real agent needs the framework. Skipped rather than stubbed, for the same
# reason the privacy file gives: a stub here would be the shortcut this evidence
# exists to avoid.
pytest.importorskip("langchain")

from app import agent, config, main, policy_tool, trace  # noqa: E402

# --- the gateway's real minting code, loaded by path -----------------------
#
# `parents[2]` is `services/`: this file, then `tests/`, then `loan-assistant/`.
# ASSERTED rather than trusted, because getting it wrong does not fail -- it
# makes the skip below fire and every test in this file disappear. That is
# exactly what happened while this was written (`parents[3]`, the repository
# root), and the container run reported eleven skips instead of a broken path.
_SERVICES_DIR = pathlib.Path(__file__).resolve().parents[2]
assert _SERVICES_DIR.name == "services", (
    "expected parents[2] to be the services directory, got " + str(_SERVICES_DIR))

_GATEWAY_AGENT_TRACE = _SERVICES_DIR / "gateway" / "app" / "agent_trace.py"


def _load_gateway_agent_trace():
    """Import the gateway's module without importing the gateway's package.

    It depends on nothing but the standard library and `langsmith`, so it loads
    standalone -- which matters, because both services have a package called
    `app` and importing the gateway's normally here would collide with this one.
    """
    spec = importlib.util.spec_from_file_location(
        "meridian_gateway_agent_trace", _GATEWAY_AGENT_TRACE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


gateway_trace = (_load_gateway_agent_trace()
                 if _GATEWAY_AGENT_TRACE.exists() else None)

# Only a genuinely single-service image reaches this skip: CI checks out the
# whole repository, so the file is there and these tests run.
pytestmark = pytest.mark.skipif(
    gateway_trace is None,
    reason="the gateway's agent_trace module is not on disk in this image")


# --- sentinels, one per prohibited category --------------------------------
S_APP_ID = 987654321
S_NAME = "Sentinel Q. Borrower"
S_APPLICANT = "APPLICANTDATA-SENTINEL-J1"
S_PROMPT = "PROMPT-SENTINEL-J2"
S_MODEL_OUT = "MODELOUTPUT-SENTINEL-J3"
S_QUERY = "POLICYQUERY-SENTINEL-J4"
S_RETRIEVED = "RETRIEVEDTEXT-SENTINEL-J5"
S_CREDENTIAL = "lsv2_pt_FAKE_SENTINEL_CREDENTIAL_J6"
#: What a caller might put in `baggage` to reach these spans from outside.
S_BAGGAGE = "INBOUNDBAGGAGE-SENTINEL-J7"

PROHIBITED = {
    "application identifier": str(S_APP_ID),
    "applicant name": S_NAME,
    "application data": S_APPLICANT,
    "prompt": S_PROMPT,
    "model output": S_MODEL_OUT,
    "policy query": S_QUERY,
    "retrieved policy text": S_RETRIEVED,
    "credential": S_CREDENTIAL,
    "inbound baggage metadata": S_BAGGAGE,
}

APP_DATA = {
    "id": S_APP_ID, "applicant_name": S_NAME, "amount": 18000,
    "term_months": 48, "purpose": "debt consolidation " + S_APPLICANT,
    "income": 72000, "employment_years": 6, "status": "under_review",
}

SUMMARY_JSON = json.dumps({
    "loan_amount": 18000, "term_months": 48, "purpose": "debt consolidation",
    "summary": "Adequate income. " + S_MODEL_OUT, "flags": [],
})

HIT = json.dumps({
    "status": "hit", "hit_count": 1,
    "excerpts": [{"document": "fee_schedule.md",
                  "version": "sha256:1972040e71e5",
                  "chunk_id": "fee_schedule.md#2.0",
                  "excerpt": S_RETRIEVED,
                  "citation": "fee_schedule.md#2.0 (sha256:1972040e71e5)"}]})

#: What the emitted tree must contain, named here so a deleted stage fails.
#: `gateway_entry` is the gateway's run; the six below are this service's.
GATEWAY_STAGE = "gateway_entry"
SERVICE_ROOT = "underwriting_summary"
EXPECTED_STAGES = {"request", "agent_run", "policy_retrieval", "model",
                   "validation", "outcome"}


class _Sink:
    """Stands in for the LangSmith endpoint. Both services post here, which is
    what makes the join observable in one place."""

    def __init__(self):
        self.body = bytearray()
        captured = self.body

        class _H(BaseHTTPRequestHandler):
            def do_POST(self):
                captured.extend(
                    self.rfile.read(int(self.headers.get("Content-Length", 0))))
                self.send_response(202)
                self.end_headers()
                self.wfile.write(b"{}")

            do_PATCH = do_POST
            do_PUT = do_POST

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

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
        self._srv.shutdown()
        self._srv.server_close()


class _Resp:
    status_code = 200

    def json(self):
        return APP_DATA

    def raise_for_status(self):
        pass


@pytest.fixture
def sink(monkeypatch):
    s = _Sink()
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", s.url)
    monkeypatch.setenv("LANGSMITH_API_KEY", S_CREDENTIAL)
    monkeypatch.setenv("LANGSMITH_PROJECT", "join-regression")
    monkeypatch.setattr(config, "LANGSMITH_API_KEY", S_CREDENTIAL)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())
    yield s
    s.close()


@pytest.fixture
def client():
    return TestClient(main.app, raise_server_exceptions=False)


def _real_agent_over_a_fake_model(monkeypatch, answer=SUMMARY_JSON):
    """Fake ONLY the external model; everything downstream of the route is real.

    Lifted deliberately from `test_trace_is_privacy_safe.py`, which was corrected
    in review for stubbing `run_underwriting_agent` -- the function that records
    the `model` and `agent_run` stages. Stubbing it means the instrumentation
    under test never executes and the wire assertions describe a trace those
    stages never contributed to.
    """
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.outputs import ChatGeneration, ChatResult
    from langchain_core.tools import StructuredTool

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

    tool = StructuredTool.from_function(
        func=lambda query, top_k=3: HIT, name=policy_tool.TOOL_NAME,
        description="Search the client's underwriting policy documents.")

    runtime = create_agent(model=_FakeModel(messages=iter([])), tools=[tool],
                           system_prompt="system contract " + S_PROMPT)
    monkeypatch.setattr(agent, "build_agent", lambda *a, **k: runtime)
    return turns


def _through_the_gateway(extra_headers=None):
    """The context the gateway actually mints, from the gateway's real code.

    This is the whole point of importing that module: the headers under test are
    the ones the gateway produces, not a hand-written imitation of them that
    would keep passing after the real format changed.
    """
    headers, root = gateway_trace.start_root(role="underwriter",
                                             route_class="agent_summary")
    headers = dict(headers)
    if extra_headers:
        headers.update(extra_headers)
    return headers, root


def _post(client, headers):
    sent = {"X-User-Role": "underwriter"}
    sent.update(headers)
    return client.post("/applications/{}/summary".format(S_APP_ID), headers=sent)


def _runs(blob):
    """Every run object in the posted bytes.

    The sink receives multipart ingest bodies, so the JSON objects are embedded
    in a stream rather than being the whole payload. Scanned with the JSON
    decoder rather than a regex: `raw_decode` consumes exactly one value and
    stops, which is what makes it safe to walk a mixed stream, and a regex over
    nested braces would either miss runs or invent them.
    """
    decoder = json.JSONDecoder()
    found = []
    for index, ch in enumerate(blob):
        if ch != "{":
            continue
        try:
            value, _ = decoder.raw_decode(blob, index)
        except ValueError:
            continue
        if isinstance(value, dict) and "trace_id" in value and "id" in value:
            found.append(value)
    return found


def _by_name(runs):
    """Runs keyed by name, keeping the first sighting of each.

    A run is posted and then patched, so the same id arrives more than once; the
    identity fields are set at creation and do not change between the two.
    """
    out = {}
    for run in runs:
        name = run.get("name")
        if name and name not in out:
            out[name] = run
    return out


def _leaks(blob):
    return {label: s for label, s in PROHIBITED.items() if s in blob}


# --------------------------------------------------------- 0. the contract

def test_both_services_agree_on_which_headers_carry_the_context():
    """The one fact that has to be identical in two codebases.

    The gateway strips and mints these names; this service joins on them. A
    disagreement is silent in both directions: the gateway would strip a header
    this service still honours (so a caller-chosen context gets through), or mint
    one this service ignores (so the trace splits back into two and nobody
    notices until an operator goes looking).
    """
    assert gateway_trace.PROPAGATION_HEADERS == trace.PROPAGATION_HEADERS


# --------------------------------------------------------------- 1. the tree

def test_the_gateway_run_is_the_root_of_the_agent_trace(sink, client, monkeypatch):
    """One trace, not two.

    The assertion is on parentage and trace id, because that is what decides
    whether an operator opening the gateway's run can see the model call
    underneath it, or has to find a second trace and take it on trust that the
    two describe the same request.
    """
    _real_agent_over_a_fake_model(monkeypatch)
    headers, root = _through_the_gateway()

    resp = _post(client, headers)
    assert resp.status_code == 200, resp.text
    gateway_trace.finish_root(root, resp.status_code)

    named = _by_name(_runs(sink.text()))
    assert GATEWAY_STAGE in named, (
        "the gateway's run never reached the sink, so there is nothing to join")
    assert SERVICE_ROOT in named, "this service's run never reached the sink"

    entry, service = named[GATEWAY_STAGE], named[SERVICE_ROOT]
    assert entry.get("parent_run_id") in (None, ""), (
        "gateway_entry is not the root of the trace")
    assert service["parent_run_id"] == entry["id"], (
        "this service's run is not parented on the gateway's -- the trace still "
        "starts one hop downstream of the authenticated entry point")
    assert service["trace_id"] == entry["trace_id"], (
        "same parent, different trace: the two runs would not appear together")


def test_every_stage_hangs_beneath_the_gateway_root(sink, client, monkeypatch):
    """The stages join too, not just the run that owns them.

    Parenting the service run correctly while leaving its children on a separate
    trace would show an operator an entry point with nothing under it -- which
    looks like a request that vanished, the exact failure the emitter's
    all-exits-record-an-outcome rule was written for.
    """
    _real_agent_over_a_fake_model(monkeypatch)
    headers, root = _through_the_gateway()

    resp = _post(client, headers)
    gateway_trace.finish_root(root, resp.status_code)

    named = _by_name(_runs(sink.text()))
    entry = named[GATEWAY_STAGE]
    for stage in EXPECTED_STAGES:
        assert stage in named, "stage " + stage + " is missing from the wire"
        assert named[stage]["trace_id"] == entry["trace_id"], (
            "stage " + stage + " was emitted on a different trace")
        assert named[stage]["parent_run_id"] == named[SERVICE_ROOT]["id"], (
            "stage " + stage + " is not beneath this service's run")


def test_the_dotted_order_nests_the_service_under_the_gateway(
        sink, client, monkeypatch):
    """`dotted_order` is what the UI actually nests on.

    Asserted separately from `parent_run_id` because they can disagree: a run
    with the right parent id and a root-level dotted_order renders as a sibling
    of the entry point rather than a child of it, and the trace looks flat to the
    person the client asked to give a trace to.
    """
    _real_agent_over_a_fake_model(monkeypatch)
    headers, root = _through_the_gateway()

    resp = _post(client, headers)
    gateway_trace.finish_root(root, resp.status_code)

    named = _by_name(_runs(sink.text()))
    entry_order = named[GATEWAY_STAGE]["dotted_order"]
    assert named[SERVICE_ROOT]["dotted_order"].startswith(entry_order)
    for stage in EXPECTED_STAGES:
        assert named[stage]["dotted_order"].startswith(entry_order)


# ------------------------------------------------------------- 2. the stages

def test_the_declared_stages_and_no_others(sink, client, monkeypatch):
    """The stage set is asserted exactly, so a rename cannot pass quietly.

    Named against `trace.STAGES` as well as the literal set: the literal is what
    the client was promised, and the module constant is what the code believes,
    and a change that moves one without the other is the drift this catches.
    """
    _real_agent_over_a_fake_model(monkeypatch)
    headers, root = _through_the_gateway()

    resp = _post(client, headers)
    gateway_trace.finish_root(root, resp.status_code)

    named = _by_name(_runs(sink.text()))
    stage_runs = {name for name in named
                  if named[name].get("parent_run_id") == named[SERVICE_ROOT]["id"]}

    assert stage_runs == EXPECTED_STAGES
    assert set(trace.STAGES) == EXPECTED_STAGES, (
        "trace.STAGES and this test's expectation have drifted apart")


def test_the_gateway_stage_is_not_a_renamed_request_stage(sink, client, monkeypatch):
    """`request` still exists and still means this service's ingress.

    Renaming it `gateway_entry` would have satisfied a search for the word while
    leaving the trace starting exactly where it did before. Both names must be
    present, on different runs, in different services.
    """
    _real_agent_over_a_fake_model(monkeypatch)
    headers, root = _through_the_gateway()

    resp = _post(client, headers)
    gateway_trace.finish_root(root, resp.status_code)

    named = _by_name(_runs(sink.text()))
    assert "request" in named and GATEWAY_STAGE in named
    assert named["request"]["id"] != named[GATEWAY_STAGE]["id"]
    assert named[GATEWAY_STAGE]["id"] not in (
        named["request"].get("parent_run_id"),)  # request is not the gateway run


# -------------------------------------------------------------- 3. the scrub

def test_joining_a_trace_leaks_nothing(sink, client, monkeypatch):
    """The join did not widen what travels.

    The privacy file made this claim about an unparented trace. Parenting adds a
    second service and two headers to the path, so the claim is re-made here
    rather than assumed to carry over.
    """
    _real_agent_over_a_fake_model(monkeypatch)
    headers, root = _through_the_gateway()

    resp = _post(client, headers)
    gateway_trace.finish_root(root, resp.status_code)

    blob = sink.text()
    assert _leaks(blob) == {}, "prohibited values on the wire: " + repr(_leaks(blob))


def test_inbound_baggage_metadata_does_not_reach_these_spans(
        sink, client, monkeypatch):
    """A hostile parent context cannot write into this service's spans.

    Verified against the installed SDK: `create_child` merges a parent's metadata
    into its children, and `RunTree.from_headers` builds that metadata out of the
    `baggage` header. So an inbound context is a way past the allow-list from
    outside -- the allow-list governs what this module ATTACHES, and this arrives
    already attached.

    The gateway strips inbound copies of both headers, so this shape cannot occur
    through the front door. It is asserted here anyway, because a defence that
    exists in one service and is assumed in the other is a defence that
    disappears the day someone reaches this service another way.
    """
    _real_agent_over_a_fake_model(monkeypatch)
    hostile = {"baggage": "langsmith-metadata=" + json.dumps(
        {"leaked": S_BAGGAGE}).replace(" ", "")}
    headers, root = _through_the_gateway(extra_headers=hostile)

    resp = _post(client, headers)
    assert resp.status_code == 200
    gateway_trace.finish_root(root, resp.status_code)

    blob = sink.text()
    assert S_BAGGAGE not in blob, (
        "inbound baggage metadata reached the trace, so the allow-list can be "
        "bypassed from outside the process")


def test_a_hostile_project_name_does_not_redirect_the_runs(
        sink, client, monkeypatch):
    """`baggage` also carries a project name.

    An accepted one would file Meridian's runs under a project of the sender's
    choosing -- which is not a content leak, but it does mean the audit trail the
    client asked for can be moved somewhere nobody is looking.
    """
    _real_agent_over_a_fake_model(monkeypatch)
    hostile = {"baggage": "langsmith-project=attacker-project"}
    headers, root = _through_the_gateway(extra_headers=hostile)

    resp = _post(client, headers)
    gateway_trace.finish_root(root, resp.status_code)

    assert "attacker-project" not in sink.text()


def test_a_hostile_project_is_refused_with_no_project_configured(
        sink, client, monkeypatch):
    """Review finding LS-PROJECT-SCRUB, and the configuration that ships.

    The scrub used to read `config.LANGSMITH_PROJECT or parent.session_name`. With
    `LANGSMITH_PROJECT` set -- which the fixture above did -- the left side won and
    the test passed. With it UNSET, which is what `.env.example` ships, the `or`
    fell through and handed the choice back to the caller. So the guarded branch
    was being asserted and the unguarded one was being claimed.

    This is the unguarded one: no project configured, a hostile project in
    `baggage`, and the runs must still not go there.
    """
    for name in ("LANGSMITH_PROJECT", "LANGCHAIN_PROJECT"):
        monkeypatch.delenv(name, raising=False)
    _real_agent_over_a_fake_model(monkeypatch)

    headers, root = _through_the_gateway(
        extra_headers={"baggage": "langsmith-project=attacker-project"})

    resp = _post(client, headers)
    assert resp.status_code == 200
    gateway_trace.finish_root(root, resp.status_code)

    assert "attacker-project" not in sink.text(), (
        "with no project configured, the inbound project name was accepted")


def test_the_project_comes_from_this_process_not_the_sender(monkeypatch):
    """The resolver, directly.

    Asserted on the function as well as through the wire test: the wire test can
    only show the value that was used, and this shows the rule -- a configured
    project wins, and with none configured the answer is the SDK default, never
    anything that arrived.
    """
    for name in ("LANGSMITH_PROJECT", "LANGCHAIN_PROJECT"):
        monkeypatch.delenv(name, raising=False)
    assert trace._own_project() == trace._SDK_DEFAULT_PROJECT

    monkeypatch.setenv("LANGSMITH_PROJECT", "meridian-real-project")
    assert trace._own_project() == "meridian-real-project"


def test_a_malformed_parent_context_still_emits_a_trace(sink, client, monkeypatch):
    """An unusable context degrades to an unparented trace, not to no trace.

    Losing the trace on a bad header would make observability the thing that
    breaks first under exactly the conditions an operator needs it.
    """
    _real_agent_over_a_fake_model(monkeypatch)

    resp = _post(client, {"langsmith-trace": "not-a-trace-context"})
    assert resp.status_code == 200

    named = _by_name(_runs(sink.text()))
    assert SERVICE_ROOT in named
    for stage in EXPECTED_STAGES:
        assert stage in named


def test_no_context_at_all_still_emits_a_trace(sink, client, monkeypatch):
    """The pre-existing behaviour is preserved.

    A request that reached this service without a gateway context -- tracing off
    upstream, or a direct call -- gets the trace it got before this change.
    """
    _real_agent_over_a_fake_model(monkeypatch)

    resp = _post(client, {})
    assert resp.status_code == 200

    named = _by_name(_runs(sink.text()))
    assert SERVICE_ROOT in named
    assert named[SERVICE_ROOT].get("parent_run_id") in (None, "")


# -------------------------------------------------------- guard the guard

def test_the_sink_would_see_the_baggage_without_the_scrub(
        sink, client, monkeypatch):
    """The scrub is what stops the leak, shown by removing it.

    Without this, `test_inbound_baggage_metadata_does_not_reach_these_spans`
    could be green because the SDK happened not to merge, because the sentinel
    never travelled, or because the parent was never parsed at all. Here
    `_inbound_parent` keeps the metadata it normally drops, and the same request
    must leak -- so a scrub that silently stopped scrubbing cannot leave this
    file green.
    """
    _real_agent_over_a_fake_model(monkeypatch)

    real = trace._inbound_parent

    def _unscrubbed(headers):
        if not headers:
            return None
        from langsmith.run_trees import RunTree

        parent = RunTree.from_headers(headers)
        return parent  # metadata, tags and project all left in place

    monkeypatch.setattr(trace, "_inbound_parent", _unscrubbed)
    assert trace._inbound_parent is not real

    hostile = {"baggage": "langsmith-metadata=" + json.dumps(
        {"leaked": S_BAGGAGE}).replace(" ", "")}
    headers, root = _through_the_gateway(extra_headers=hostile)

    _post(client, headers)
    gateway_trace.finish_root(root, 200)

    assert S_BAGGAGE in sink.text(), (
        "the sentinel did not appear even with the scrub disabled, so the scrub "
        "test proves nothing about the scrub")
