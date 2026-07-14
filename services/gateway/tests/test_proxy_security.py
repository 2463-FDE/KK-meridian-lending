"""Regression test for the anonymous /los/* header-spoofing leak.

The gateway's /los/{path} route proxies anonymously when no session is present
(applicants can check status without an account). _proxy used to forward the
client's raw headers verbatim, including any X-User-Role the client sent
itself -- so an anonymous caller could spoof X-User-Role: admin and have
origination-service's staff-only /financials route trust it. _proxy now strips
inbound X-User-* headers before (optionally) setting trusted ones from the
resolved session.
"""
import json

import httpx
from fastapi.testclient import TestClient

from app.main import app


class _FakeResponse:
    def __init__(self, status_code, json_body):
        self.status_code = status_code
        self._json_body = json_body
        self.text = json.dumps(json_body)

    def json(self):
        return self._json_body


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient and mimics origination-service's own
    staff-only check, so the test proves the *gateway* never lets a spoofed
    role reach it as if it were trusted."""

    last_headers = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def request(self, method, url, content=None, headers=None, params=None):
        _FakeAsyncClient.last_headers = headers
        # Downstream (Starlette-based, like origination-service) reads headers
        # case-insensitively regardless of the wire casing ASGI gives us here.
        role = httpx.Headers(headers or {}).get("X-User-Role")
        if role not in ("csr", "underwriter", "admin"):
            return _FakeResponse(403, {"detail": "staff only"})
        return _FakeResponse(200, {"income": 95000, "employment_years": 12})


def test_anonymous_financials_request_with_spoofed_role_header_is_rejected(monkeypatch):
    monkeypatch.setattr("app.main.httpx.AsyncClient", _FakeAsyncClient)
    client = TestClient(app)

    resp = client.get(
        "/los/applications/1/financials",
        headers={"X-User-Role": "admin"},  # attacker-supplied, no session/token
    )

    assert resp.status_code == 403
    # The spoofed header must never have been forwarded as-is.
    forwarded = httpx.Headers(_FakeAsyncClient.last_headers or {})
    assert forwarded.get("X-User-Role") != "admin"
