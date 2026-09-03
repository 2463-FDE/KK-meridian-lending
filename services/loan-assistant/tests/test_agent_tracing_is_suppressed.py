"""Enabling LangSmith tracing cannot leak the agent's content. Measured.

The client's prohibited-retention list includes prompts, responses, queries and
retrieved text. On this branch, before the guard existed, one agent run with
`LANGSMITH_TRACING=true` posted ~31KB to the LangSmith endpoint containing all
four. Nothing in the code prevented it -- the framework traces by default and a
line in `.env.example` is not a control.

So the guarantee is asserted the only way that means anything: point LangSmith
at a local sink, run the real `run_underwriting_agent` path, and read what was
actually transmitted. `test_the_sink_would_catch_a_leak` runs the identical
graph WITHOUT the guard and shows the sentinels arriving, so a suppression that
silently stopped working could not leave this file green.

No paid calls: the model is a fake, and the sink is a socket on localhost.
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytest.importorskip("langchain")
pytest.importorskip("langsmith")

from app import agent, policy_tool  # noqa: E402

#: Distinctive enough that a substring match cannot be a coincidence, and each
#: one stands for a different item on the client's prohibited list.
APPLICANT = "APPLICANT-SENTINEL-DOB-1979 income 72000"
SYSTEM = "SYSTEM-SENTINEL-CONTRACT"
TOOL_QUERY = "TOOLQUERY-SENTINEL-late-fee"
MODEL_OUTPUT = "MODELOUT-SENTINEL-9001"

SUMMARY = json.dumps({
    "loan_amount": 18000, "term_months": 48, "purpose": "debt consolidation",
    "summary": MODEL_OUTPUT + " adequate income.", "flags": [],
})


class _Sink:
    """A stand-in LangSmith ingest endpoint that keeps every byte it is sent."""

    def __init__(self):
        self.body = bytearray()
        captured = self.body

        class _Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                captured.extend(self.rfile.read(length))
                self.send_response(202)
                self.end_headers()
                self.wfile.write(b"{}")

            do_PATCH = do_POST

            def do_GET(self):
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        self._server = HTTPServer(("127.0.0.1", 0), _Handler)
        self.url = "http://127.0.0.1:{}".format(self._server.server_port)
        threading.Thread(target=self._server.serve_forever, daemon=True).start()

    def text(self):
        _flush_langsmith()
        return bytes(self.body).decode("utf-8", "replace")

    def close(self):
        self._server.shutdown()
        self._server.server_close()


def _flush_langsmith():
    """LangSmith batches in a background thread; unflushed is not 'suppressed'."""
    import langsmith

    try:
        langsmith.client.Client().flush()
    except Exception:  # pragma: no cover - flush is best effort
        pass
    # The batcher wakes on an interval, so give it one.
    time.sleep(2)


@pytest.fixture
def sink(monkeypatch):
    s = _Sink()
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", s.url)
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2-not-a-real-key-local-sink")
    monkeypatch.setenv("LANGSMITH_PROJECT", "suppression-regression-test")
    yield s
    s.close()


def _fake_agent():
    """The real LangChain graph and the real tool, with a fake model.

    Not a stub of the agent: the graph, the tool node and the message history
    are genuine, because those are what the tracer serialises. A stubbed agent
    would trace nothing and the test would pass vacuously.
    """
    from langchain.agents import create_agent
    from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
    from langchain_core.messages import AIMessage
    from langchain_core.tools import StructuredTool

    class _ToolCapableFake(GenericFakeChatModel):
        def bind_tools(self, tools, **kwargs):
            return self

    turns = iter([
        AIMessage(content="", tool_calls=[
            {"name": policy_tool.TOOL_NAME, "args": {"query": TOOL_QUERY},
             "id": "call-1"}]),
        AIMessage(content=SUMMARY),
    ])
    tool = StructuredTool.from_function(
        func=policy_tool.search_underwriting_policy,
        name=policy_tool.TOOL_NAME,
        description="Search the client's underwriting policy documents.")

    return create_agent(model=_ToolCapableFake(messages=turns),
                        tools=[tool], system_prompt=SYSTEM)


def _leaks(blob):
    return {
        "prompt/applicant data": APPLICANT in blob,
        "system prompt": SYSTEM in blob,
        "tool query": TOOL_QUERY in blob,
        "model output": MODEL_OUTPUT in blob,
    }


# --------------------------------------------------------------------------
# The guarantee.
# --------------------------------------------------------------------------

def test_the_agent_path_transmits_nothing_with_tracing_enabled(sink):
    text, state = agent.run_underwriting_agent(APPLICANT, agent=_fake_agent())

    assert agent.required_tool_was_called(state), "the run must be a real one"
    assert MODEL_OUTPUT in text, "the run must have produced the summary"

    blob = sink.text()
    leaked = _leaks(blob)
    assert not any(leaked.values()), (
        "prohibited content reached the trace endpoint: {}".format(leaked))
    assert blob == "", (
        "the agent path transmitted {} bytes to LangSmith".format(len(blob)))


def test_the_sink_would_catch_a_leak(sink):
    """Guard the guard.

    Identical graph, identical sentinels, invoked WITHOUT `suppressed_tracing`
    -- which is precisely the pre-guard behaviour. If this stops leaking, the
    test above has become decorative and the assertion here says so.
    """
    _fake_agent().invoke({"messages": [{"role": "user", "content": APPLICANT}]},
                         config={"recursion_limit": 12})

    leaked = _leaks(sink.text())
    assert leaked["prompt/applicant data"], (
        "the sink caught nothing, so the suppression test proves nothing: "
        "{}".format(leaked))
    assert leaked["system prompt"] and leaked["tool query"], leaked


# --------------------------------------------------------------------------
# The switch the guard reads.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", ["LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2",
                                  "LANGCHAIN_TRACING"])
@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", "on"])
def test_every_spelling_of_the_tracing_switch_is_recognised(monkeypatch, name, value):
    """Checking only LANGSMITH_TRACING would leave the LANGCHAIN_* names open."""
    for other in agent._TRACING_ENV:
        monkeypatch.delenv(other, raising=False)
    monkeypatch.setenv(name, value)

    assert agent.tracing_is_requested() is True


@pytest.mark.parametrize("value", ["", "  ", "false", "0", "no", "off"])
def test_tracing_off_is_not_misread_as_on(monkeypatch, value):
    for other in agent._TRACING_ENV:
        monkeypatch.delenv(other, raising=False)
    monkeypatch.setenv("LANGSMITH_TRACING", value)

    assert agent.tracing_is_requested() is False


def test_suppression_is_a_no_op_when_tracing_is_off(monkeypatch, caplog):
    """No warning, no behaviour change -- the common case stays quiet."""
    import logging

    for other in agent._TRACING_ENV:
        monkeypatch.delenv(other, raising=False)

    with caplog.at_level(logging.DEBUG):
        with agent.suppressed_tracing():
            pass

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "framework tracing suppressed" not in logged


def test_enabling_tracing_says_so_in_the_log(monkeypatch, caplog):
    """Someone who enabled tracing and sees no FRAMEWORK spans must be able to
    find out why without reading this file.

    The marker moved with the message (TRC-01). It used to read
    `stage=privacy_interim reason=no_privacy_safe_emitter_yet`, which stopped
    being true once `app/trace.py` existed and was wired into the summary route
    -- the summary DOES emit a privacy-safe run, so a reader following that
    message would have concluded nothing was emitted at all.
    """
    import logging

    monkeypatch.setenv("LANGSMITH_TRACING", "true")

    # INFO, not WARNING: suppressing the framework while emitting a custom
    # privacy-safe trace is the intended arrangement, not a degradation. What
    # this case pins is that the log SAYS SO -- silence is the failure, because
    # then the only way to learn why LangSmith shows no framework spans is to
    # read the source (TRC-01).
    with caplog.at_level(logging.INFO):
        with agent.suppressed_tracing():
            pass

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "framework tracing suppressed" in logged
    # The reason has to name the emitter, or the message explains nothing.
    assert "custom_privacy_safe_emitter_in_use" in logged
    assert "app.trace.summary_trace" in logged
    # And the old, now-false reason must not come back (TRC-01).
    assert "no_privacy_safe_emitter_yet" not in logged
    assert APPLICANT not in logged
