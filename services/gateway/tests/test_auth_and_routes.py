"""Characterization tests for the gateway's auth endpoints and proxy authz gates.

Pins CURRENT behavior (as of this session) before any Week 4+ change touches this
service -- gateway had only test_proxy_security.py before (one spoofed-header
regression test), nothing covering /auth/* or the differing authz gates across
/los, /lss, /payments, /assistant. Not a redesign: /lss and /payments intentionally
require authentication but NOT a specific role (debt D8, documented in
app/main.py's module docstring, fixed in Week 6) -- these tests pin that gap as it
exists today, they don't close it.
"""
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from app import auth, main
from app.main import app

client = TestClient(app)


class _FakeResponse:
    def __init__(self, status_code, json_body):
        self.status_code = status_code
        self._json_body = json_body
        self.text = json.dumps(json_body)

    def json(self):
        return self._json_body


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient so proxy tests never need a live
    downstream service -- records the request it received and returns a fixed
    200 body, mirroring test_proxy_security.py's existing fake."""

    last_url = None
    last_headers = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def request(self, method, url, content=None, headers=None, params=None):
        _FakeAsyncClient.last_url = url
        _FakeAsyncClient.last_headers = headers
        return _FakeResponse(200, {"ok": True})


# --- /health ------------------------------------------------------------------

def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok", "service": "gateway"}


# --- /auth/login ----------------------------------------------------------

def test_login_success_returns_token_and_user(monkeypatch):
    user = {"id": 2, "username": "underwriter", "role": "underwriter", "name": "Sam Okafor"}
    monkeypatch.setattr(auth, "authenticate", lambda u, p: user)
    monkeypatch.setattr(auth, "create_session", lambda u: "faketoken123")

    resp = client.post("/auth/login", json={"username": "underwriter", "password": "password"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["token"] == "faketoken123"
    assert body["user"] == user


def test_login_invalid_credentials_is_401(monkeypatch):
    monkeypatch.setattr(auth, "authenticate", lambda u, p: None)

    resp = client.post("/auth/login", json={"username": "nobody", "password": "wrong"})

    assert resp.status_code == 401


def test_login_backend_error_is_503(monkeypatch):
    def _boom(u, p):
        raise RuntimeError("db down")

    monkeypatch.setattr(auth, "authenticate", _boom)

    resp = client.post("/auth/login", json={"username": "underwriter", "password": "password"})

    assert resp.status_code == 503


# --- /auth/me / /auth/logout ------------------------------------------------

def test_me_with_valid_session_returns_user(monkeypatch):
    user = {"id": 2, "username": "underwriter", "role": "underwriter", "name": "Sam Okafor"}
    monkeypatch.setattr(auth, "get_session", lambda token: user)

    resp = client.get("/auth/me", headers={"Authorization": "Bearer faketoken123"})

    assert resp.status_code == 200
    assert resp.json() == user


def test_me_with_no_session_is_401(monkeypatch):
    monkeypatch.setattr(auth, "get_session", lambda token: None)

    resp = client.get("/auth/me")

    assert resp.status_code == 401


def test_logout_calls_delete_session(monkeypatch):
    calls = []
    monkeypatch.setattr(auth, "delete_session", lambda token: calls.append(token))

    resp = client.post("/auth/logout", headers={"Authorization": "Bearer faketoken123"})

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    assert calls == ["faketoken123"]


# --- proxy authz gates (current state -- see module docstring above) -------

def test_los_proxies_anonymously_with_no_session(monkeypatch):
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)

    resp = client.get("/los/applications/1")

    assert resp.status_code == 200


def test_lss_requires_authentication(monkeypatch):
    monkeypatch.setattr(auth, "get_session", lambda token: None)

    resp = client.get("/lss/loans/1")

    assert resp.status_code == 401


def test_lss_accepts_any_authenticated_role(monkeypatch):
    # Characterizes the CURRENT gap (debt D8): /lss requires auth but does not
    # check role at all -- a borrower session reaches servicing same as staff.
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(auth, "get_session", lambda token: {
        "id": 1, "username": "maria", "role": "borrower", "name": "Maria Gonzalez",
    })

    resp = client.get("/lss/loans/1", headers={"Authorization": "Bearer faketoken123"})

    assert resp.status_code == 200


def test_payments_requires_authentication(monkeypatch):
    monkeypatch.setattr(auth, "get_session", lambda token: None)

    resp = client.post("/payments/charge", json={})

    assert resp.status_code == 401


def test_assistant_requires_authentication(monkeypatch):
    monkeypatch.setattr(auth, "get_session", lambda token: None)

    resp = client.post("/assistant/policy-chat", json={"question": "x"})

    assert resp.status_code == 401


def test_assistant_rejects_non_staff_role(monkeypatch):
    monkeypatch.setattr(auth, "get_session", lambda token: {
        "id": 1, "username": "maria", "role": "borrower", "name": "Maria Gonzalez",
    })

    resp = client.post(
        "/assistant/policy-chat",
        json={"question": "x"},
        headers={"Authorization": "Bearer faketoken123"},
    )

    assert resp.status_code == 403


@pytest.mark.parametrize("role", ["csr", "underwriter", "admin"])
def test_assistant_accepts_staff_roles(monkeypatch, role):
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(auth, "get_session", lambda token: {
        "id": 2, "username": "x", "role": role, "name": "X",
    })

    resp = client.post(
        "/assistant/policy-chat",
        json={"question": "x"},
        headers={"Authorization": "Bearer faketoken123"},
    )

    assert resp.status_code == 200


def test_proxy_strips_inbound_authorization_header(monkeypatch):
    # The client's own Authorization header (the gateway session token) must
    # never be forwarded downstream verbatim -- _proxy explicitly drops it.
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(auth, "get_session", lambda token: None)

    client.get("/los/applications/1", headers={"Authorization": "Bearer faketoken123"})

    forwarded = httpx.Headers(_FakeAsyncClient.last_headers or {})
    assert "authorization" not in {k.lower() for k in forwarded.keys()}
