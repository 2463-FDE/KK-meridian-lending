"""What the auto-offer graph puts on the wire, measured rather than reasoned about.

`app/disclosure_graph.py` runs the two-agent auto-offer flow as a LangGraph
`StateGraph`, and LangGraph brings `langchain-core`, which instruments every
`invoke` the moment `LANGSMITH_TRACING` and `LANGSMITH_API_KEY` are set. Both are
set in every deployed environment here, from the shared `.env`, pointed at a real
project -- nothing had to be wired up for it to happen.

The payload is the graph state: `app_id`, the approved amount and term in
`decision_inputs`, and the assembled `offer`. An application identifier and a
borrower's approved loan terms are both on the client's prohibited-retention list.

Found by scanning for the sibling of a gap that was reported in another service.
The brief named decision-service's graph; this one was not named and has the same
mechanism, so it is measured and fixed on the same terms rather than left because
nobody mentioned it.

The assertion is zero bytes, with the real graph running against a local sink, and
the guard-the-guard test at the bottom removes the suppression and shows the same
run leaking. Only the two external steps are faked -- the knowledge-graph read and
the HTTP call to disclosure-service -- because those reach a database and another
service. The graph, its nodes and its edges are the real code, and one test
asserts the offer that comes out is unchanged.
"""
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

pytest.importorskip("langsmith")
pytest.importorskip("langgraph")

from app import clients, disclosure_graph, kg, tracing  # noqa: E402

S_APP_ID = 5150
S_AMOUNT = 24500
S_TERM = 60
S_OFFER_ID = "OFFERID-SENTINEL-B1"
S_APR = "APRSENTINEL-11.4271"

PROHIBITED = {
    "application id": str(S_APP_ID),
    "approved amount": str(S_AMOUNT),
    "term": str(S_TERM),
    "offer id": S_OFFER_ID,
    "disclosed apr": S_APR,
}

APPROVED_INPUTS = {
    "app_id": S_APP_ID,
    "amount": S_AMOUNT,
    "term_months": S_TERM,
}

OFFER = {
    "offer_id": S_OFFER_ID,
    "apr": S_APR,
    "application_id": S_APP_ID,
    "principal": S_AMOUNT,
    "term_months": S_TERM,
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

        Both flush paths are tried, because flushing a freshly constructed client
        would drain an empty queue and hand back an empty sink -- which would make
        every assertion below pass for the wrong reason.
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


@pytest.fixture
def tracing_on(monkeypatch):
    """Tracing switched fully on, aimed at a local sink.

    Deliberately ON: with it off the suppression is a no-op and zero bytes would
    be the framework's doing rather than this code's.
    """
    sink = _Sink()
    for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"):
        monkeypatch.setenv(name, "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_FAKE_FOR_TEST")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", sink.url)
    monkeypatch.setenv("LANGCHAIN_ENDPOINT", sink.url)
    monkeypatch.setenv("LANGSMITH_PROJECT", "auto-offer-suppression-regression")
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
def no_database_and_no_disclosure_call(monkeypatch):
    """The two external steps, faked. The graph is not."""
    monkeypatch.setattr(kg, "get_approved_decision_inputs",
                        lambda app_id: dict(APPROVED_INPUTS))
    monkeypatch.setattr(clients, "post",
                        lambda url, path, body, headers=None: dict(OFFER))


def _leaks(blob):
    return {label: s for label, s in PROHIBITED.items() if s in blob}


# ------------------------------------------------------------------ the claim

def test_an_auto_offer_posts_nothing_at_all(tracing_on):
    disclosure_graph.auto_generate_offer(S_APP_ID)

    blob = tracing_on.text()
    assert blob == "", (
        "the auto-offer graph transmitted {} bytes with tracing enabled".format(
            len(blob)))


def test_no_prohibited_value_reaches_the_wire(tracing_on):
    disclosure_graph.auto_generate_offer(S_APP_ID)

    blob = tracing_on.text()
    assert _leaks(blob) == {}, "prohibited values on the wire: " + repr(
        sorted(_leaks(blob)))


def test_the_skipped_path_posts_nothing_either(tracing_on, monkeypatch):
    """The branch where no approved decision exists.

    A separate path through the graph, and the one whose state carries the
    `skipped` message -- which is built from the app_id. Covered because "the
    happy path is clean" says nothing about the branch that formats an identifier
    into a string.
    """
    monkeypatch.setattr(kg, "get_approved_decision_inputs", lambda app_id: None)

    assert disclosure_graph.auto_generate_offer(S_APP_ID) is None

    blob = tracing_on.text()
    assert blob == ""
    assert _leaks(blob) == {}


# --------------------------------------------------------- the offer is intact

def test_the_offer_that_comes_out_is_unchanged(tracing_on):
    """A privacy fix must not touch the money.

    The suppression wraps the invoke and changes nothing inside it, and this says
    so: the same offer disclosure-service returned is the one the caller gets.
    """
    offer = disclosure_graph.auto_generate_offer(S_APP_ID)

    assert offer == OFFER


def test_the_same_offer_comes_out_with_tracing_off(monkeypatch):
    """Suppression is not a behaviour switch."""
    for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"):
        monkeypatch.delenv(name, raising=False)
    off = disclosure_graph.auto_generate_offer(S_APP_ID)

    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_FAKE_FOR_TEST")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "http://127.0.0.1:9")
    on = disclosure_graph.auto_generate_offer(S_APP_ID)

    assert off == on == OFFER


# ---------------------------------------------------------- the switch itself

def test_every_spelling_of_the_tracing_flag_is_recognised(monkeypatch):
    for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"):
        monkeypatch.delenv(name, raising=False)
    assert not tracing.tracing_is_requested()

    for name in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2", "LANGCHAIN_TRACING"):
        monkeypatch.setenv(name, "true")
        assert tracing.tracing_is_requested(), name + " was not recognised"
        monkeypatch.delenv(name)


def test_suppression_refuses_rather_than_leaking_if_it_cannot_suppress(
        monkeypatch):
    """Fail closed.

    The caller treats auto-offer generation as best-effort, so refusing costs a
    convenience feature and a loan officer can still build the offer by hand --
    a smaller price than transmitting an application id and approved terms.
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


def test_the_suppression_log_line_carries_no_application_values(tracing_on,
                                                                caplog):
    with caplog.at_level("WARNING"):
        disclosure_graph.auto_generate_offer(S_APP_ID)

    suppressed = [r.getMessage() for r in caplog.records
                  if "tracing suppressed" in r.getMessage()]
    assert suppressed, "the suppression was silent"
    for message in suppressed:
        for label, sentinel in PROHIBITED.items():
            assert sentinel not in message, (
                "the suppression log line leaked the " + label)


# ------------------------------------------------------- guard the guard

def test_the_sink_would_see_the_state_without_the_suppression(tracing_on,
                                                              monkeypatch):
    """The suppression is what stops the leak, shown by removing it.

    Without this, the zero-bytes assertions could hold because the exporter never
    ran, because the sink was unreachable, or because `langchain-core` stopped
    instrumenting `invoke` at some version -- none of which is the suppression
    working.
    """
    import contextlib

    @contextlib.contextmanager
    def _no_suppression():
        yield

    monkeypatch.setattr(disclosure_graph, "suppressed_tracing", _no_suppression)

    disclosure_graph.auto_generate_offer(S_APP_ID)

    blob = tracing_on.text()
    assert blob, "nothing was posted even with suppression removed, so these " \
                 "tests prove nothing about the suppression"
    assert _leaks(blob), (
        "no prohibited value appeared even with suppression removed -- the "
        "zero-bytes assertions above are not measuring what they claim")
