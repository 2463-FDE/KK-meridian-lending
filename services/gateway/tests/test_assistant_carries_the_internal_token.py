"""The gateway must prove to loan-assistant that a request came through it.

The other half of SEC-16. loan-assistant's staff summary route now refuses a
caller that presents only `X-User-Role`, which is correct and which also means
the gateway has to send the thing that is no longer optional: every other
`/los/*` proxy already carries `X-Internal-Token`, and `/assistant/*` did not.

Worth stating plainly, because the two halves fail in opposite directions and
only one of them is loud:

  * without the downstream check, a role header alone gets staff data (SEC-16);
  * without this header, every AI Summary in the product 403s.

The second is the one that would be found in a demo rather than in a review, so
it gets a test rather than a comment. This exact pairing has already gone wrong
once here -- `loan-assistant/app/config.py` records the round where
origination-service began requiring the token and this service was not sending
it, and every summary request 403'd until it did.

`/assistant/policy-chat` is deliberately not covered: it is registered as its
own route ahead of this catch-all, is anonymous-allowed on purpose, and
loan-assistant's policy-chat route requires no token. A test asserting it
carries one would be asserting the wrong contract.
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import auth
from app.config import INTERNAL_SERVICE_TOKEN
from app.main import app

USER_ID = 7


class _FakeResponse:
    def __init__(self, status_code, json_body):
        self.status_code = status_code
        self._json_body = json_body
        self.text = json.dumps(json_body)
        self.content = self.text.encode("utf-8")

    def json(self):
        return self._json_body


class _Recorder:
    """Stands in for httpx.AsyncClient and mimics loan-assistant's own check."""

    last_headers = None
    last_url = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def request(self, method, url, content=None, headers=None, params=None):
        _Recorder.last_headers = headers
        _Recorder.last_url = url
        h = httpx.Headers(headers or {})
        # loan-assistant's _require_staff, in miniature: both halves or 403.
        if h.get("X-Internal-Token") != INTERNAL_SERVICE_TOKEN:
            return _FakeResponse(403, {"detail": "staff only"})
        if h.get("X-User-Role") not in ("csr", "underwriter", "admin"):
            return _FakeResponse(403, {"detail": "staff only"})
        return _FakeResponse(200, {"summary": "ok", "risk_tier": "low"})


@pytest.fixture
def staff_client(monkeypatch):
    monkeypatch.setattr(auth, "get_session",
                        lambda token: {"id": USER_ID, "role": "underwriter"} if token else None)
    monkeypatch.setattr("app.main.httpx.AsyncClient", _Recorder)
    c = TestClient(app)
    c.headers.update({"Authorization": "Bearer session-token"})
    return c


def test_the_summary_proxy_sends_the_internal_token(staff_client):
    resp = staff_client.post("/assistant/applications/42/summary")

    assert resp.status_code == 200, (
        "the staff summary path is broken: %d %s" % (resp.status_code, resp.text))
    sent = httpx.Headers(_Recorder.last_headers or {})
    assert sent.get("X-Internal-Token") == INTERNAL_SERVICE_TOKEN, (
        "the gateway did not prove the request came through it, so every AI "
        "Summary would 403 against a loan-assistant that requires the token")
    assert sent.get("X-User-Role") == "underwriter", (
        "the resolved role no longer reaches loan-assistant")


def test_the_caller_cannot_supply_the_internal_token_themselves(staff_client):
    """The token is minted here, not accepted here.

    A caller who could set it would be able to reach loan-assistant directly
    with a role of their choosing -- reintroducing SEC-16 through the front
    door instead of the network.
    """
    staff_client.post("/assistant/applications/42/summary",
                      headers={"X-Internal-Token": "attacker-supplied",
                               "X-User-Role": "admin"})

    sent = httpx.Headers(_Recorder.last_headers or {})
    assert sent.get("X-Internal-Token") == INTERNAL_SERVICE_TOKEN, (
        "a caller-supplied X-Internal-Token survived the proxy hop")
    assert sent.get("X-User-Role") == "underwriter", (
        "a caller-supplied X-User-Role survived the proxy hop and would have "
        "been presented downstream as a resolved role")


def test_a_borrower_never_reaches_loan_assistant_at_all(monkeypatch):
    """The gateway refuses first, so the token is never spent on a non-staff
    caller. Asserting the upstream was not called is the point: a 403 produced
    downstream would still have handed a borrower's request the token."""
    monkeypatch.setattr(auth, "get_session",
                        lambda token: {"id": 9, "role": "borrower"} if token else None)
    monkeypatch.setattr("app.main.httpx.AsyncClient", _Recorder)
    _Recorder.last_url = None

    c = TestClient(app)
    resp = c.post("/assistant/applications/42/summary",
                  headers={"Authorization": "Bearer session-token"})

    assert resp.status_code == 403
    assert _Recorder.last_url is None, (
        "a borrower's request was forwarded to loan-assistant carrying the "
        "internal token before being refused")


def test_an_anonymous_caller_never_reaches_loan_assistant(monkeypatch):
    monkeypatch.setattr(auth, "get_session", lambda token: None)
    monkeypatch.setattr("app.main.httpx.AsyncClient", _Recorder)
    _Recorder.last_url = None

    c = TestClient(app)
    resp = c.post("/assistant/applications/42/summary",
                  headers={"X-User-Role": "admin"})

    assert resp.status_code == 401
    assert _Recorder.last_url is None
