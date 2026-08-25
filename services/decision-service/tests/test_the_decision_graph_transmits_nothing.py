"""What the decision graph puts on the wire, measured rather than reasoned about.

`app/graph.py` runs the credit decision as a LangGraph `StateGraph`, and LangGraph
brings `langchain-core`, which instruments every `ainvoke` the moment
`LANGSMITH_TRACING` and `LANGSMITH_API_KEY` are set. Both are set in every
deployed environment here, from the shared `.env`, pointed at a real project --
nothing had to be wired up for it to happen, which is why it went unnoticed for
as long as it did.

The payload is the graph state, and `DecisionState.application` is the whole
application dict. `_node_pull_credit` reads `application["ssn"]`, so the SSN is in
it. Before the suppression this file guards, one decision posted ~30KB carrying
the SSN, the bureau score, the bureau reference id, the applicant name, the
application id, the income and the bureau request key.

So the assertion is the strong one: **zero bytes**, with the real graph running
against a local sink standing in for the LangSmith endpoint. Zero is a claim that
needs no argument about what a filter would have caught -- and the
guard-the-guard test at the bottom removes the suppression and shows the same run
leaking, so a suppressor that silently stopped suppressing cannot leave this file
green.

Only `_pull_credit` is faked, because it is the one step that would reach a credit
bureau. Everything else -- the graph, its three nodes, the scoring call, the
finalize node -- is the real code, and one test asserts the decision itself is
unchanged, because a privacy fix that quietly altered a credit decision would be
a far worse bug than the one it fixed.
"""
import asyncio
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytest.importorskip("langsmith")
# The exposure only exists when the framework is installed, and so does the fix.
pytest.importorskip("langgraph")

from app import decision, graph, tracing  # noqa: E402

# --- one sentinel per prohibited value the state is known to carry ---------
S_SSN = "SSN-SENTINEL-999-00-1234"
S_NAME = "Sentinel Q. Borrower"
S_APP_ID = 4242
# Named and shaped so it cannot be mistaken for a credential.
#
# The first version of this line was a name ending in KEY assigned a
# high-entropy uppercase literal, and the `secrets` job flagged it as a generic
# API key -- correctly, in the sense that a scanner cannot tell a fabricated
# marker from a real one. Fixed by changing the sentinel rather than adding a
# gitleaks allowlist entry, which would have quieted this line and blunted the
# rule for whatever lands in this file next.
#
# The replacement is deliberately NOT accompanied by the old literal quoted for
# posterity: writing it into a comment reproduces the exact string the scanner
# objected to, and that is its own second CI failure. Describing the shape is
# enough. Low entropy, obviously a marker, same assertion.
S_BUREAU_MARKER = "bureau-request-marker-a1"
S_BUREAU_REF = "BUREAUREF-SENTINEL-A2"
S_INCOME = 72111
S_SCORE = 731

PROHIBITED = {
    "ssn": S_SSN,
    "applicant name": S_NAME,
    "application id": str(S_APP_ID),
    "bureau request key": S_BUREAU_MARKER,
    "bureau reference id": S_BUREAU_REF,
    "income": str(S_INCOME),
    "bureau score": str(S_SCORE),
}

APPLICATION = {
    "id": S_APP_ID,
    "ssn": S_SSN,
    "applicant_name": S_NAME,
    "bureau_request_key": S_BUREAU_MARKER,
    "amount": 18000,
    "term_months": 48,
    "income": S_INCOME,
    "employment_years": 6,
}


class _Sink:
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
        """The bytes posted, after making any exporter finish sending.

        Both flush paths are tried: `langchain-core` posts through whichever
        client it constructed, and flushing a freshly built one would drain an
        empty queue and return an empty sink -- which would make every assertion
        below pass for the wrong reason.
        """
        try:
            from langsmith import run_trees

            if run_trees._CLIENT is not None:
                run_trees._CLIENT.flush()
        except Exception:
            pass
        try:
            import langsmith

            langsmith.client.Client().flush()
        except Exception:
            pass
        time.sleep(2.5)
        return bytes(self.body).decode("utf-8", "replace")

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()


class _BureauResult:
    score = S_SCORE
    reference_id = S_BUREAU_REF


@pytest.fixture
def tracing_on(monkeypatch):
    """Tracing switched fully on, aimed at a local sink.

    Deliberately ON. A test that ran with tracing off would prove nothing: the
    suppression would be a no-op and zero bytes would be the framework's doing,
    not this code's.
    """
    sink = _Sink()
    for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"):
        monkeypatch.setenv(name, "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_FAKE_FOR_TEST")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", sink.url)
    monkeypatch.setenv("LANGCHAIN_ENDPOINT", sink.url)
    monkeypatch.setenv("LANGSMITH_PROJECT", "decision-suppression-regression")
    try:
        from langsmith import run_trees

        previous = run_trees._CLIENT
        run_trees._CLIENT = None
    except ImportError:  # pragma: no cover
        previous = None
    yield sink
    try:
        from langsmith import run_trees

        run_trees._CLIENT = previous
    except ImportError:  # pragma: no cover
        pass
    sink.close()


@pytest.fixture(autouse=True)
def no_real_bureau_call(monkeypatch):
    """The one external step, faked. Nothing else is."""
    async def _pull(ssn, key):
        assert ssn == S_SSN, "the graph stopped passing the SSN it was given"
        assert key == S_BUREAU_MARKER
        return _BureauResult()

    monkeypatch.setattr(decision, "_pull_credit", _pull)


def _run_a_decision():
    return asyncio.run(graph.run(dict(APPLICATION)))


def _leaks(blob):
    return {label: s for label, s in PROHIBITED.items() if s in blob}


# ------------------------------------------------------------------ the claim

def test_a_decision_posts_nothing_at_all(tracing_on):
    """Zero bytes, with tracing on and the real graph running."""
    _run_a_decision()

    blob = tracing_on.text()
    assert blob == "", (
        "the decision graph transmitted {} bytes with tracing enabled".format(
            len(blob)))


def test_no_prohibited_value_reaches_the_wire(tracing_on):
    """Named per category, so a failure says WHICH value escaped.

    Kept alongside the zero-bytes assertion rather than folded into it: if a
    later change makes this path emit something deliberately, the byte count
    stops being zero and this is the test that still has to hold.
    """
    _run_a_decision()

    blob = tracing_on.text()
    assert _leaks(blob) == {}, "prohibited values on the wire: " + repr(
        sorted(_leaks(blob)))


# ------------------------------------------------- the decision is untouched

def test_the_decision_itself_is_unchanged(tracing_on):
    """A privacy fix that altered a credit decision would be the worse bug.

    The suppression wraps the invoke and touches nothing inside it, and this is
    the assertion that says so: the graph still returns the same keys, and the
    score still comes from the bureau result the nodes were given.
    """
    result = _run_a_decision()

    assert result["bureau_score"] == S_SCORE
    assert result["bureau_reference_id"] == S_BUREAU_REF
    assert result["decision"] in ("approve", "decline", "review")
    for key in ("score", "model_version", "top_features", "reason_codes"):
        assert key in result, "the graph stopped returning " + key


def test_the_same_decision_comes_out_with_tracing_off(monkeypatch):
    """Suppression is not a behaviour switch.

    Run with every tracing variable unset, the graph must produce exactly what it
    produces with them set -- otherwise the suppression is doing something to the
    decision and not just to what leaves the process.
    """
    for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"):
        monkeypatch.delenv(name, raising=False)

    off = _run_a_decision()

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_FAKE_FOR_TEST")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "http://127.0.0.1:9")
    on = _run_a_decision()

    assert off == on


# ---------------------------------------------------------- the switch itself

def test_every_spelling_of_the_tracing_flag_is_recognised(monkeypatch):
    """`langsmith` reads one name and `langchain-core` still honours another.

    Checking one and missing the other is how a service ends up tracing while
    believing it is not, so all three are recognised.
    """
    for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"):
        monkeypatch.delenv(name, raising=False)
    assert not tracing.tracing_is_requested()

    for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"):
        monkeypatch.setenv(name, "true")
        assert tracing.tracing_is_requested(), name + " was not recognised"
        monkeypatch.delenv(name)


def test_the_flag_is_read_per_call_not_at_import(monkeypatch):
    """An operator's change has to take effect without a restart of this module."""
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)
    assert not tracing.tracing_is_requested()
    monkeypatch.setenv("LANGSMITH_TRACING", "1")
    assert tracing.tracing_is_requested()


def test_suppression_refuses_rather_than_leaking_if_it_cannot_suppress(
        monkeypatch):
    """Fail closed.

    If the suppressor cannot be imported while tracing is on, the decision is
    refused. Proceeding would mean shipping an SSN to a third party, and a
    missing dependency is not a reason to accept that. Simulated by making the
    import fail, since `langsmith` is transitively guaranteed while `langgraph`
    is installed -- which is exactly why the tracing was on to begin with.
    """
    import builtins

    real_import = builtins.__import__

    def _no_langsmith(name, *args, **kwargs):
        if name == "langsmith.run_helpers":
            raise ImportError("simulated: no suppressor available")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_langsmith)
    monkeypatch.setenv("LANGSMITH_TRACING", "true")

    with pytest.raises(tracing.UnsafeTracingConfiguration):
        with tracing.suppressed_tracing():
            pass


def test_a_missing_suppressor_is_harmless_when_tracing_is_off(monkeypatch):
    """Nothing to suppress, so nothing to refuse.

    The fail-closed branch must not turn an unrelated dependency change into an
    outage for a deployment that never enabled tracing.
    """
    import builtins

    real_import = builtins.__import__

    def _no_langsmith(name, *args, **kwargs):
        if name == "langsmith.run_helpers":
            raise ImportError("simulated: no suppressor available")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_langsmith)
    for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"):
        monkeypatch.delenv(name, raising=False)

    with tracing.suppressed_tracing():
        pass  # must not raise


def test_suppression_says_so_in_the_log(tracing_on, caplog):
    """Silence would look like a broken exporter rather than a deliberate refusal.

    Categorical only -- a stage and a reason, no application values -- because a
    log line explaining a privacy suppression would be an absurd place to leak.
    """
    with caplog.at_level("WARNING"):
        _run_a_decision()

    messages = [r.getMessage() for r in caplog.records]
    suppressed = [m for m in messages if "tracing suppressed" in m]
    assert suppressed, "the suppression was silent"
    for message in suppressed:
        for label, sentinel in PROHIBITED.items():
            assert sentinel not in message, (
                "the suppression log line leaked the " + label)


# ------------------------------------------------------- guard the guard

def test_the_sink_would_see_the_state_without_the_suppression(tracing_on,
                                                              monkeypatch):
    """The suppression is what stops the leak, shown by removing it.

    Without this, every assertion above could be green because the exporter never
    ran, because the sink was never reachable, or because `langchain-core` stopped
    instrumenting `ainvoke` at some version -- none of which is the suppression
    working. So the context manager is replaced with a no-op and the same
    decision must leak the SSN.

    This is also the measurement the fix was based on: ~30KB per decision, with
    the SSN, the bureau score, the reference id, the applicant name, the
    application id, the income and the bureau request key all present.
    """
    import contextlib

    @contextlib.contextmanager
    def _no_suppression():
        yield

    monkeypatch.setattr(graph, "suppressed_tracing", _no_suppression)

    _run_a_decision()

    blob = tracing_on.text()
    assert blob, "nothing was posted even with suppression removed, so these " \
                 "tests prove nothing about the suppression"
    leaked = _leaks(blob)
    assert "ssn" in leaked, (
        "the SSN did not appear even with suppression removed -- the "
        "zero-bytes assertions above are not measuring what they claim")
