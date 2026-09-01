"""SEC-13: an upstream body this gateway cannot read must not reach the caller.

`_proxy` used to end:

    except Exception:
        return JSONResponse(status_code=resp.status_code,
                            content={"raw": resp.content.decode(..., errors="replace")})

Every service behind this gateway is FastAPI and answers with JSON, so a body
that is not JSON is by definition unexpected -- and unexpected bodies are
exactly the ones worth not forwarding. An HTML error page from something in
front of a service, a stack trace from a crashed worker, a plain-text message
naming an internal host: the more broken the estate is, the more the body tends
to say, and `{"raw": ...}` handed all of it to whoever asked.

WHAT THIS FILE PINS

  1. No unreadable upstream body reaches the caller, in any of the shapes one
     can take -- HTML, plain text, a stack trace, invalid UTF-8, a huge body, an
     empty body, a bare JSON scalar.
  2. The refusal is IDENTICAL every time, so the response itself carries no
     information about what went wrong upstream.
  3. Status semantics survive: an upstream error stays that error, an upstream
     success with an unreadable body becomes 502 rather than a 200 nobody can
     use, and a body-less status stays body-less.
  4. Readable JSON is still passed through untouched, including at error
     statuses -- this must not become a proxy that swallows real 4xx detail.
  5. The diagnosis lives in the LOG, bounded: service, status, content-type and
     body length, never the body.

Asserted through the real `_proxy`, with only `httpx.AsyncClient` replaced, so
what is measured is the response an external caller would actually receive.
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import auth, main


class _Resp:
    """An upstream response, exactly as httpx would present one."""

    def __init__(self, status_code, content, content_type="application/json"):
        self.status_code = status_code
        self.content = content
        self.headers = {"content-type": content_type}


@pytest.fixture
def upstream(monkeypatch):
    """Install one canned upstream response; return a box to set it in."""
    box = {"resp": _Resp(200, b'{"ok": true}')}

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, content=None, headers=None, params=None):
            return box["resp"]

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return box


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(auth, "get_session",
                        lambda token: {"id": 1, "role": "admin"} if token else None)
    return TestClient(main.app)


#: One proxied route that reaches `_proxy` with no special-casing in front of
#: it. `/los/*` forwards anything, which is what makes it the honest probe.
_ROUTE = "/los/applications/1"
_AUTH = {"Authorization": "Bearer t"}


def _get(client):
    return client.get(_ROUTE, headers=_AUTH)


# ---------------------------------------------------------------------------
# 1. Readable JSON still works, including at error statuses.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [200, 201, 400, 401, 403, 404, 409, 422, 500, 503])
def test_json_is_passed_through_at_every_status(client, upstream, status):
    """The fix must not become a proxy that swallows real upstream detail.

    A 422's validation errors and a 409's conflict message are the caller's
    means of correcting the request. Losing them would be a worse regression
    than the leak being closed.
    """
    upstream["resp"] = _Resp(status, json.dumps({"detail": "upstream says so"}).encode())

    resp = _get(client)

    assert resp.status_code == status
    assert resp.json() == {"detail": "upstream says so"}


def test_a_json_array_body_is_passed_through(client, upstream):
    """Two proxied routes return lists. An object-only rule would break them."""
    upstream["resp"] = _Resp(200, b'[{"id": 1}, {"id": 2}]')

    resp = _get(client)

    assert resp.status_code == 200
    assert resp.json() == [{"id": 1}, {"id": 2}]


def test_utf8_is_decoded_as_utf8_not_guessed(client, upstream):
    """The existing charset fix must survive this change.

    `resp.json()` decodes via `resp.text`, which lets httpx guess the charset
    when the upstream sends `application/json` with no charset -- and a guess
    turned an accented name into mojibake on every route. The bytes are decoded
    as UTF-8 directly, and this keeps that true.
    """
    upstream["resp"] = _Resp(200, '{"name": "José"}'.encode("utf-8"))

    assert _get(client).json() == {"name": "José"}


# ---------------------------------------------------------------------------
# 2. Nothing unreadable is reflected, in any shape.
# ---------------------------------------------------------------------------

_LEAKY_BODIES = [
    ("html error page",
     b"<html><head><title>502 Bad Gateway</title></head><body>"
     b"<h1>nginx/1.25.3 at origination-service:8001</h1></body></html>",
     "text/html"),
    ("plain text naming a host",
     b"upstream connect error: connection refused to servicing-service:8002",
     "text/plain"),
    ("python stack trace",
     b'Traceback (most recent call last):\n  File "/app/app/routers/x.py", '
     b"line 42, in handler\n    raise RuntimeError(secret_value)\n"
     b"RuntimeError: DATABASE_URL=postgresql://meridian:postgres@postgres:5432\n",
     "text/plain"),
    ("malformed json", b'{"detail": "unterminated', "application/json"),
    ("invalid utf-8", b'{"detail": "\xff\xfe not utf-8"}', "application/json"),
    ("empty body", b"", "application/json"),
    ("bare json scalar", b'42', "text/plain"),
    ("bare json string", b'"an internal message"', "text/plain"),
]


@pytest.mark.parametrize("label,body,content_type", _LEAKY_BODIES,
                         ids=[c[0] for c in _LEAKY_BODIES])
@pytest.mark.parametrize("status", [200, 500])
def test_an_unreadable_body_is_never_reflected(client, upstream, label, body,
                                               content_type, status):
    upstream["resp"] = _Resp(status, body, content_type)

    resp = _get(client)
    rendered = resp.content.decode("utf-8", errors="replace")

    assert resp.json() == {"detail": main._UNREADABLE_DETAIL}, label
    # Nothing recognisable from the upstream body survives -- checked on the
    # rendered response rather than on the parsed one, because a leak could hide
    # in a key as easily as in a value.
    for fragment in ("nginx", "origination-service", "servicing-service",
                     "Traceback", "RuntimeError", "DATABASE_URL", "postgresql",
                     "unterminated", "an internal message", "42"):
        assert fragment not in rendered, (
            f"{fragment!r} from an upstream {label} reached the caller")


def test_every_refusal_is_byte_identical(client, upstream):
    """The response must carry no information about which failure occurred.

    A caller that could tell an HTML page from a truncated write from invalid
    UTF-8 would be reading the estate's internal state one probe at a time.
    """
    seen = set()
    for _, body, content_type in _LEAKY_BODIES:
        upstream["resp"] = _Resp(200, body, content_type)
        resp = _get(client)
        seen.add((resp.status_code, resp.content))

    assert len(seen) == 1, seen


def test_a_large_non_json_body_is_not_reflected_and_not_echoed_by_length(
    client, upstream
):
    """A megabyte of HTML must not become a megabyte of response."""
    upstream["resp"] = _Resp(200, b"<html>" + b"A" * 1_000_000 + b"</html>",
                             "text/html")

    resp = _get(client)

    assert resp.json() == {"detail": main._UNREADABLE_DETAIL}
    assert len(resp.content) < 200, (
        "the refusal grew with the upstream body, so the body is still being "
        "reflected in some form")


# ---------------------------------------------------------------------------
# 3. Status semantics.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 422, 500, 502, 503])
def test_an_upstream_error_status_is_preserved(client, upstream, status):
    """A 404 stays a 404. The status is not the part that leaks, and a caller's
    retry decision depends on it."""
    upstream["resp"] = _Resp(status, b"<html>error</html>", "text/html")

    assert _get(client).status_code == status


@pytest.mark.parametrize("status", [200, 201, 202])
def test_an_unreadable_success_becomes_502(client, upstream, status):
    """Calling it 200 would assert the request succeeded while returning an
    error body, and a caller trusting the status would act on nothing."""
    upstream["resp"] = _Resp(status, b"<html>not json</html>", "text/html")

    assert _get(client).status_code == 502


@pytest.mark.parametrize("status", [204, 304])
def test_a_body_less_status_is_returned_as_itself(client, upstream, status):
    """Not an unreadable response -- a response with nothing in it.

    Nothing upstream returns one today, checked route by route. If one ever
    does, it must not be turned into a 502 by a rule about unparseable bodies.
    """
    upstream["resp"] = _Resp(status, b"")

    resp = _get(client)

    assert resp.status_code == status
    assert resp.content == b""


# ---------------------------------------------------------------------------
# 4. The diagnosis is in the log, and is bounded.
# ---------------------------------------------------------------------------

def test_the_log_carries_metadata_and_never_the_body(client, upstream, caplog):
    """Enough to find the upstream; nothing that would persist its content.

    A log line is a place an untrusted body would live on, so the body is not
    written there either -- content-type and length are what distinguish an
    HTML error page from a truncated write.
    """
    body = (b"Traceback (most recent call last):\n"
            b"RuntimeError: DATABASE_URL=postgresql://meridian:postgres@postgres:5432\n")
    upstream["resp"] = _Resp(200, body, "text/plain")

    with caplog.at_level("WARNING", logger="gateway"):
        _get(client)

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "unreadable upstream response" in logged
    assert "status=200" in logged
    assert "content_type=text/plain" in logged
    assert f"body_bytes={len(body)}" in logged
    for fragment in ("Traceback", "RuntimeError", "DATABASE_URL", "postgresql",
                     "meridian"):
        assert fragment not in logged, f"{fragment!r} from the body reached the log"


def test_the_log_names_a_service_label_not_an_internal_url(client, upstream, caplog):
    """An internal hostname and port is topology, and topology travels badly.

    A label says which service to go and look at, which is the whole job.
    """
    upstream["resp"] = _Resp(200, b"<html>x</html>", "text/html")

    with caplog.at_level("WARNING", logger="gateway"):
        _get(client)

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "service=origination" in logged
    assert "http://" not in logged and ":8001" not in logged


def test_a_decode_error_message_is_not_logged_either(client, upstream, caplog):
    """`JSONDecodeError` quotes the offending document in its own message, so
    the exception is logged by TYPE and never by `str(exc)`."""
    upstream["resp"] = _Resp(200, b'{"secret": "hunter2", ', "application/json")

    with caplog.at_level("WARNING", logger="gateway"):
        _get(client)

    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "hunter2" not in logged
    assert "error=" in logged
