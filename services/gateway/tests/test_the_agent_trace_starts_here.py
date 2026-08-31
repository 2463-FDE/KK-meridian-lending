"""The agent trace's root, and who is allowed to choose it.

The client asked to see one trace running from the authenticated entry point
through the agent to the final outcome. loan-assistant's trace covered the second
half and said so in its own docstring: it opened one hop downstream, so the step
that decides whether the agent runs at all -- the authentication and staff check
in this service -- was outside the picture.

This file is about the half that lives here, and it asserts three separable
things rather than one vague "tracing works":

  1. **Order.** The root run is opened AFTER authorisation. A rejected request
     produces no run, so a run existing means the request was allowed.
  2. **Ownership.** The context that travels downstream is the one this service
     minted. Inbound copies of the propagation headers do not survive the hop --
     on any route, not only this one.
  3. **Content.** What rides on the wire is categorical and drawn from a closed
     vocabulary; an identifier or a free-text value put into the root's metadata
     is dropped rather than transmitted.

The propagation headers are `langsmith-trace` and `baggage`, which is what
`RunTree.to_headers()` produces in the installed SDK (0.10.5 here, 0.11.1 in the
loan-assistant image; the header names are identical in both). They are asserted
through `agent_trace.PROPAGATION_HEADERS` rather than retyped, so a header the
SDK adds later cannot be stripped in one place and forgotten in the other.
"""
import json
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest
from fastapi.testclient import TestClient

pytest.importorskip("langsmith")

from app import agent_trace, auth  # noqa: E402
from app.main import app  # noqa: E402

SUMMARY_PATH = "/assistant/applications/4242/summary"

#: A context an attacker might send, shaped like a real one so it would actually
#: be honoured if it were forwarded.
HOSTILE_TRACE = "20260824T120000000000Zdeadbeef-dead-beef-dead-beefdeadbeef"
HOSTILE_BAGGAGE = "langsmith-metadata=%7B%22x%22%3A%22ATTACKER%22%7D"

#: Distinctive so it can be searched for on the wire. The user id is on the
#: prohibited list; the resolved ROLE is not, and travels deliberately.
USER_ID = 987654321


class _FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self.content = json.dumps(body or {"summary": "ok"}).encode("utf-8")

    def json(self):
        return json.loads(self.content)


class _Upstream:
    """Records what actually reached the downstream service."""

    calls = []

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def request(self, method, url, content=None, headers=None, params=None):
        _Upstream.calls.append(httpx.Headers(headers or {}))
        return _FakeResponse()


@pytest.fixture(autouse=True)
def upstream(monkeypatch):
    _Upstream.calls = []
    monkeypatch.setattr("app.main.httpx.AsyncClient", _Upstream)
    return _Upstream


class _Sink:
    """A local stand-in for the LangSmith endpoint, so the run bytes this service
    posts can be read rather than reasoned about.

    Pointed at a live server rather than a closed port on purpose: a refused
    connection would exercise the error path on every test and prove nothing
    about what the emitter serialises.
    """

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
        """The bytes posted so far, after making the emitter finish sending.

        Flushes the CACHED client rather than a freshly constructed one. `RunTree`
        posts through `run_trees._CLIENT`, and each client owns its own background
        queue -- so `Client().flush()` flushes a brand-new object with nothing in
        it, waits, and returns an empty sink. Every assertion of the form "these
        bytes do not contain X" then passes for the wrong reason.
        """
        try:
            from langsmith import run_trees

            if run_trees._CLIENT is not None:
                run_trees._CLIENT.flush()
        except Exception:
            pass
        time.sleep(1.5)
        return bytes(self.body).decode("utf-8", "replace")

    def close(self):
        self._srv.shutdown()
        self._srv.server_close()


@pytest.fixture(autouse=True)
def langsmith_client_is_not_shared_between_tests():
    """A client per test, reading the endpoint this test actually set.

    `RunTree` memoises one `Client` in a module global for the life of the
    process, which is right in production and wrong here: every test stands up a
    sink on a fresh port, so the first emitter binds the cached client to that
    port and every later test posts into a closed socket. The sink then reads
    empty, and a "nothing leaked" assertion holds because nothing was sent at
    all. Reset per test so the bytes are real.
    """
    try:
        from langsmith import run_trees
    except ImportError:  # pragma: no cover - langsmith is a declared dependency
        yield
        return

    previous = run_trees._CLIENT
    run_trees._CLIENT = None
    try:
        yield
    finally:
        run_trees._CLIENT = previous


@pytest.fixture
def sink():
    s = _Sink()
    yield s
    s.close()


@pytest.fixture
def tracing_on(monkeypatch, sink):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_FAKE_FOR_TEST")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", sink.url)
    monkeypatch.setenv("LANGSMITH_PROJECT", "gateway-trace-regression")
    return sink


def _session(monkeypatch, role):
    """The resolved session, stubbed at the same seam every other gateway test
    uses. `/auth/login` reads Postgres, and these tests are about what happens
    after a session exists, not about how one is obtained."""
    monkeypatch.setattr(
        auth, "get_session",
        lambda token: {"id": USER_ID, "role": role} if token else None)


@pytest.fixture
def staff_client(monkeypatch):
    _session(monkeypatch, "underwriter")
    c = TestClient(app)
    c.headers.update({"Authorization": "Bearer session-token"})
    return c


def _runs(blob):
    """Every run object in the posted bytes.

    The sink receives multipart ingest bodies, so the JSON objects sit inside a
    stream. Scanned with the JSON decoder rather than a regex: `raw_decode`
    consumes exactly one value and stops, which is what makes it safe to walk a
    mixed stream, and a regex over nested braces would either miss runs or invent
    them.
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
        if isinstance(value, dict) and "id" in value and "name" in value:
            found.append(value)
    return found


def _forwarded():
    assert _Upstream.calls, "nothing reached the upstream service"
    return _Upstream.calls[-1]


# ---------------------------------------------------------------- 1. order

def test_an_unauthenticated_request_mints_no_trace(tracing_on, monkeypatch):
    """No session, no run. The root describes an authenticated entry, so there
    is nothing for it to describe here."""
    _session(monkeypatch, "underwriter")  # a session exists; this caller has none

    resp = TestClient(app).post(SUMMARY_PATH)

    assert resp.status_code == 401
    assert not _Upstream.calls, "an unauthenticated request was proxied"
    assert agent_trace.STAGE not in tracing_on.text(), (
        "an unauthenticated request posted a gateway_entry run")


def test_a_borrower_is_refused_before_any_trace_exists(tracing_on, monkeypatch):
    """The staff check runs first, and this is the ordering assertion.

    A root opened before authorisation would mean every probe of this route
    created a run, which turns the trace into a record of attempts rather than of
    authorised work -- and hands an unauthorised caller a way to write into
    Meridian's observability project.

    **Asserted on the sink, not on the proxy.** The first version of this test
    checked only that nothing was forwarded upstream, and a mutation that moved
    `start_root` above the staff check passed it: refusing to proxy says nothing
    about whether a run was already minted and posted. The bytes are the only
    place that answer shows up.
    """
    _session(monkeypatch, "borrower")

    resp = TestClient(app).post(SUMMARY_PATH,
                                headers={"Authorization": "Bearer session-token"})

    assert resp.status_code == 403
    assert not _Upstream.calls, "a non-staff request was proxied"
    assert agent_trace.STAGE not in tracing_on.text(), (
        "a refused request posted a gateway_entry run, so the root is being "
        "opened before the authorisation decision")


# ------------------------------------------------------------ 2. ownership

def test_the_gateway_mints_the_context_it_forwards(tracing_on, staff_client):
    staff_client.post(SUMMARY_PATH)

    forwarded = _forwarded()
    for header in agent_trace.PROPAGATION_HEADERS:
        assert forwarded.get(header), (
            header + " was not forwarded, so loan-assistant has no parent to "
            "join and the trace still starts one hop downstream")


def test_an_inbound_context_does_not_survive_the_hop(tracing_on, staff_client):
    """The caller does not get to choose the trace.

    `_proxy` forwards inbound headers by default -- that is what makes this a
    real hole rather than a theoretical one. A caller-supplied context would
    parent Meridian's internal spans under a tree the caller controls: it can be
    used to group and locate internal runs, and `baggage` additionally carries
    metadata and a project name, so the sender could file our runs wherever it
    liked.
    """
    staff_client.post(SUMMARY_PATH, headers={"langsmith-trace": HOSTILE_TRACE,
                                             "baggage": HOSTILE_BAGGAGE})

    forwarded = _forwarded()
    assert forwarded.get("langsmith-trace") != HOSTILE_TRACE
    assert HOSTILE_BAGGAGE not in (forwarded.get("baggage") or "")
    assert "ATTACKER" not in json.dumps(dict(forwarded)), (
        "the caller's baggage reached the downstream service")


def test_an_inbound_context_is_stripped_when_this_service_mints_nothing(
        staff_client, monkeypatch):
    """The configuration where the strip is the ONLY thing protecting the trace.

    Deleting the strip and running the test above still passes, because on this
    route the minted headers overwrite the caller's -- masking is doing the work,
    not stripping. Turn minting off and the mask goes with it.

    This is not a contrived arrangement. `LANGSMITH_TRACING` is per service, so a
    gateway with tracing off in front of a loan-assistant with tracing on is one
    environment file away, and in that state a caller-supplied context is the
    only context there is.
    """
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)

    staff_client.post(SUMMARY_PATH, headers={"langsmith-trace": HOSTILE_TRACE,
                                             "baggage": HOSTILE_BAGGAGE})

    forwarded = _forwarded()
    assert forwarded.get("langsmith-trace") is None
    assert forwarded.get("baggage") is None


def test_an_inbound_context_is_stripped_even_where_nothing_is_traced(
        tracing_on, staff_client):
    """The strip lives in `_proxy`, so it applies to every route.

    Scoping it to the traced route would leave every other proxied path able to
    carry a caller-chosen context into a service that might later join one --
    a hole that opens by omission, in a file nobody was editing at the time.
    """
    staff_client.get("/lss/loans", headers={"langsmith-trace": HOSTILE_TRACE,
                                            "baggage": HOSTILE_BAGGAGE})

    forwarded = _forwarded()
    assert forwarded.get("langsmith-trace") is None
    assert forwarded.get("baggage") is None


def test_tracing_off_forwards_no_context_at_all(staff_client, monkeypatch):
    """Off means absent, not empty.

    Forwarding a header with no usable value would have loan-assistant try to
    join a parent that does not exist: a warning per request, and a trace that
    looks broken rather than switched off.
    """
    monkeypatch.delenv("LANGSMITH_TRACING", raising=False)

    staff_client.post(SUMMARY_PATH)

    forwarded = _forwarded()
    for header in agent_trace.PROPAGATION_HEADERS:
        assert forwarded.get(header) is None


def test_only_the_summary_route_opens_a_run(tracing_on, staff_client):
    """`/assistant/health` is not agent work.

    Matched with an anchored, closed pattern like the servicing matchers in this
    service rather than a prefix test: a prefix would also claim paths this
    service knows nothing about, and each one would mint a run named for work
    that never happened.
    """
    staff_client.get("/assistant/health")

    forwarded = _forwarded()
    for header in agent_trace.PROPAGATION_HEADERS:
        assert forwarded.get(header) is None, (
            "a health check opened an agent trace")


# -------------------------------------------------------------- 3. content

def test_the_root_carries_only_allowlisted_categorical_fields(tracing_on):
    headers, root = agent_trace.start_root(role="underwriter",
                                           route_class="agent_summary")
    assert root is not None
    assert set(headers) == set(agent_trace.PROPAGATION_HEADERS)
    assert root.extra["metadata"] == {
        "stage": "gateway_entry",
        "service": "gateway",
        "role": "underwriter",
        "route_class": "agent_summary",
        "tracing_mode": "privacy_safe_categorical",
        "schema_version": agent_trace.SCHEMA_VERSION,
    }
    assert root.inputs == {}, "the request body must not reach the trace"


def test_a_field_outside_the_allow_list_is_dropped():
    """Structural, so a field nobody anticipated does not travel.

    A denylist has to name what it forbids; the value that leaks is the one added
    later by someone who did not read this file.
    """
    kept = agent_trace._safe({
        "stage": "gateway_entry",
        "applicant_id": 5582,
        "application_id": 4242,
        "user_id": "u-1",
        "email": "borrower@example.com",
        "amount": 18000,
        "authorization": "Bearer secret",
        "path": "/assistant/applications/4242/summary",
    })

    assert kept == {"stage": "gateway_entry"}


def test_a_value_outside_its_vocabulary_is_dropped():
    """A categorical-looking key is not a licence to carry free text."""
    kept = agent_trace._safe({"role": "underwriter; note=FREETEXT",
                              "service": "gateway"})

    assert kept == {"service": "gateway"}


def test_a_structured_value_is_dropped_whatever_its_key(tracing_on):
    """Dicts and lists are the shapes that carry payloads.

    `role` is an allowed key, so this is the case where the key passes and the
    value must still not: an allow-list that checked only names would transmit
    an entire application under a field called `role`.
    """
    kept = agent_trace._safe({"role": {"name": "Sentinel Q. Borrower"},
                              "service": ["gateway", "and-a-payload"]})

    assert kept == {}


def test_the_run_id_is_random_and_derived_from_nothing(tracing_on):
    """The observability id identifies a request and answers nothing else.

    Two runs for the same role and route must not share an id, and the id must
    not be reconstructible from anything about the request -- it is a uuid4, so
    the assertion is that it is one and that it differs across calls.
    """
    ids = set()
    for _ in range(5):
        _, root = agent_trace.start_root(role="underwriter",
                                         route_class="agent_summary")
        ids.add(str(root.id))
        assert uuid.UUID(str(root.id)).version == 4

    assert len(ids) == 5, (
        "run ids repeat, so they encode something about the request")


#: A dotted-order segment: a microsecond timestamp followed by a run uuid.
#: `RunTree.to_headers()` builds `langsmith-trace` out of these, so the header is
#: mostly machine-generated opacity.
_GENERATED = re.compile(
    r"\d{8}T\d{6}\d*Z?"                                  # 20260830T234819123456Z
    r"|[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"      # a dashed uuid
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
    # Bounded by NON-HEX rather than by a word boundary: in a dotted-order
    # segment the run id follows the timestamp's `Z`, which is itself a word
    # character, so \b never fires there and the id went unmasked. The
    # false positive this whole helper exists for survived the first fix
    # because of it, and the reproduction case is what caught that.
    r"|(?<![0-9a-fA-F])[0-9a-fA-F]{32}(?![0-9a-fA-F])"    # or an undashed one
)


def _scannable(blob: str) -> str:
    """The forwarded context with its GENERATED identifiers removed.

    A leak probe must not be able to cry wolf, and this one could. The check is
    a substring search for short request identifiers -- `4242` among them --
    over a string that is mostly random hex: a run uuid has 32 hex characters,
    so any given four-digit decimal string appears in one by chance roughly once
    in two thousand ids, and CI mints several per run. It duly failed on `main`
    at 4c5bb13 with `assert '4242' not in '20260830T23...'`, reporting a leak
    that had not happened.

    A security assertion that fires at random is worse than none, because the
    response to it is to rerun the job.

    Removing the generated segments cannot hide a real leak: they are minted
    here from a uuid4 and a clock, and nothing about the request reaches them --
    `test_run_ids_do_not_encode_the_request` asserts exactly that. Anything the
    application actually attached travels in `baggage`, which is left intact,
    and `test_the_masking_does_not_hide_a_real_leak` proves the masking does not
    swallow one.
    """
    return _GENERATED.sub("", blob)


def test_the_masking_does_not_hide_a_real_leak(tracing_on, staff_client):
    """The probe above only means something if it still catches a leak.

    Written because the fix to a false positive is exactly the kind of change
    that quietly disarms the check it was meant to keep honest: mask a little
    too much and the assertion passes forever.
    """
    staff_client.post(SUMMARY_PATH)
    blob = "".join(_forwarded().get(h) or ""
                   for h in agent_trace.PROPAGATION_HEADERS)

    for planted in ("4242", str(USER_ID), "Bearer abc", "applications/4242"):
        assert planted in _scannable(blob + planted), (
            f"masking swallowed {planted!r}; the leak probe would no longer "
            "catch it")


def test_a_generated_id_that_happens_to_contain_a_request_number_is_not_a_leak():
    """The false positive itself, reproduced so it cannot come back.

    This is the exact shape that failed on `main` at 4c5bb13: a dotted-order
    segment whose random hex happens to contain `4242`. No request identifier
    leaked; the digits are a coincidence of uuid4, and the old probe called it a
    leak.
    """
    coincidence = "20260830T234819123456Z" + "a1b24242c3d4e5f60718293a4b5c6d7e"
    assert "4242" in coincidence, "the fixture must contain the digits"
    assert "4242" not in _scannable(coincidence), (
        "a generated identifier containing the digits by chance is still being "
        "reported as a leaked request id")


def test_the_forwarded_context_contains_no_request_identifiers(
        tracing_on, staff_client):
    """Read off the header that actually travels, not off the object.

    `baggage` is a serialised carrier and the prohibition is about what leaves
    this process, so the assertion is made on the bytes.
    """
    staff_client.post(SUMMARY_PATH)

    forwarded = _forwarded()
    blob = "".join(forwarded.get(h) or ""
                   for h in agent_trace.PROPAGATION_HEADERS)
    # The application id, the user id, the credential and the URL. Note what is
    # NOT probed for: the substring "summary", because the allowlisted
    # `route_class` value is literally `agent_summary` -- a probe that forbade it
    # would be forbidding the categorical field this design chose to send, which
    # is a test asserting against its own contract rather than a leak check.
    for forbidden in ("4242", str(USER_ID), "Bearer", "applications/"):
        assert forbidden not in _scannable(blob), (
            forbidden + " rides on the trace context"
        )


# ------------------------------------------------- the root always closes

def test_the_root_is_ended_when_the_upstream_call_raises(tracing_on,
                                                        staff_client,
                                                        monkeypatch):
    """Review finding GTRACE-OPEN-ROOT.

    `_proxy` makes an outbound HTTP call, so an upstream timeout or a refused
    connection leaves the route by exception. The root has already been posted by
    then, and `finish_root` used to sit after the await -- so on that path the run
    was never ended and an operator saw a root with no outcome, which looks like a
    request still in flight rather than one that failed. That is the precise
    confusion this trace exists to remove.

    Asserted on the bytes: the run must arrive with an end time.
    """
    class _Exploding:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, *a, **k):
            raise httpx.ConnectError("simulated: loan-assistant unreachable")

    monkeypatch.setattr("app.main.httpx.AsyncClient", _Exploding)

    with pytest.raises(httpx.ConnectError):
        staff_client.post(SUMMARY_PATH)

    blob = tracing_on.text()
    assert agent_trace.STAGE in blob, "the root was never posted at all"
    runs = [r for r in _runs(blob) if r.get("name") == agent_trace.STAGE]
    assert runs, "no gateway_entry run in the posted bytes"
    assert any(r.get("end_time") for r in runs), (
        "the gateway_entry run was posted but never ended, so the trace shows a "
        "root that never finished")


def test_a_failed_hop_is_told_apart_from_an_upstream_that_answered(
        tracing_on, staff_client, monkeypatch):
    """A status code alone cannot say which happened.

    An upstream returning 502 and a proxy that raised before any response existed
    read identically as `http_status: 502`. The categorical `status` separates
    them, so an operator counting failures is not counting two different events
    as one.
    """
    class _Exploding:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, *a, **k):
            raise httpx.ReadTimeout("simulated: upstream timed out")

    monkeypatch.setattr("app.main.httpx.AsyncClient", _Exploding)

    with pytest.raises(httpx.ReadTimeout):
        staff_client.post(SUMMARY_PATH)

    blob = tracing_on.text()
    assert '"status":"error"' in blob.replace(" ", ""), (
        "a hop that never completed was not marked as an error")


def test_no_exception_text_reaches_the_trace(tracing_on, staff_client,
                                             monkeypatch):
    """An error path is where a raw provider error would try to travel.

    Raw provider errors are on the prohibited list, and the closing path is the
    one that runs when something went wrong -- so the sentinel goes in the
    exception message and must not appear in the bytes.
    """
    sentinel = "PROVIDERERROR-SENTINEL-G9 credential=abcdef"

    class _Exploding:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def request(self, *a, **k):
            raise httpx.ConnectError(sentinel)

    monkeypatch.setattr("app.main.httpx.AsyncClient", _Exploding)

    with pytest.raises(httpx.ConnectError):
        staff_client.post(SUMMARY_PATH)

    assert sentinel not in tracing_on.text()
    assert "PROVIDERERROR-SENTINEL-G9" not in tracing_on.text()


def test_a_normal_response_still_records_its_status(tracing_on, staff_client):
    """The success path did not regress into the failure branch."""
    staff_client.post(SUMMARY_PATH)

    blob = tracing_on.text().replace(" ", "")
    assert '"status":"ok"' in blob
    assert '"http_status":200' in blob


# ------------------------------------------------------- guard the guard

def test_the_strip_test_would_notice_if_the_strip_were_removed(
        tracing_on, staff_client, monkeypatch):
    """Guard the guard, on the assertion that matters most.

    `test_an_inbound_context_does_not_survive_the_hop` passes if the gateway
    replaces the caller's context. It would ALSO pass if the gateway happened to
    overwrite it for some unrelated reason -- so this removes the strip and the
    minting, and confirms the hostile value then arrives intact. If this test
    fails, the strip assertion above is proving nothing about the strip.
    """
    monkeypatch.setattr(agent_trace, "PROPAGATION_HEADERS", ())
    monkeypatch.setattr(agent_trace, "start_root", lambda **k: ({}, None))

    staff_client.post(SUMMARY_PATH, headers={"baggage": HOSTILE_BAGGAGE})

    assert "ATTACKER" in (_forwarded().get("baggage") or ""), (
        "the hostile baggage did not arrive even with the strip disabled, so "
        "the strip test proves nothing about the strip")


def test_an_emitter_failure_does_not_fail_the_request(tracing_on, staff_client):
    """Observability may not break underwriting.

    The endpoint in `tracing_on` points at a closed port, so delivery fails on
    every test in this file. The summary must still be returned -- the same rule
    loan-assistant's emitter follows.
    """
    resp = staff_client.post(SUMMARY_PATH)

    assert resp.status_code == 200
