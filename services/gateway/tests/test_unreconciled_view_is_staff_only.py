"""Who may read the list of borrowers whose money is in limbo.

`payment-service` can now say WHICH payments were captured on a card and never
credited to a loan balance, not just how many. That list has to leave the
compose network for it to be any use -- the gauges page a person, and the person
was previously reduced to psql -- and this gateway is the only door out.

So the question this file settles is who gets through it. The listing names
other people's payments: a borrower must never read it, and an anonymous caller
must not reach payment-service at all, because this gateway attaches the
internal token itself and forwarding an unauthenticated request means signing it
on the caller's behalf -- the shape of the `/kyc/*` bypass that
`test_kyc_proxy_requires_staff.py` exists for.

Asserted on what REACHES payment-service, not on the status code. A gateway that
forwarded the request and relayed a 403 back would satisfy a status assertion
while having already leaked the data.
"""
import httpx
import pytest
from fastapi.testclient import TestClient

from app import auth, main


@pytest.fixture
def upstream(monkeypatch):
    """Records anything that reaches a downstream service."""
    seen = []

    class _Response:
        status_code = 200
        content = b'{"total": 0, "returned": 0, "truncated": false, "items": []}'

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def request(self, method, url, content=None, headers=None, params=None):
            seen.append({"method": method, "url": url, "headers": dict(headers or {})})
            return _Response()

    monkeypatch.setattr(httpx, "AsyncClient", _Client)
    return seen


@pytest.fixture
def client():
    return TestClient(main.app)


def _session(monkeypatch, role):
    monkeypatch.setattr(
        auth, "get_session",
        lambda token: ({"id": 1, "role": role} if token else None))


_PATHS = ("/payments/unreconciled", "/payments/unreconciled/items")


@pytest.mark.parametrize("path", _PATHS)
def test_an_anonymous_caller_never_reaches_payment_service(client, upstream,
                                                           monkeypatch, path):
    monkeypatch.setattr(auth, "get_session", lambda token: None)

    resp = client.get(path)

    assert resp.status_code == 401
    assert upstream == [], (
        "an unauthenticated read reached payment-service through the gateway, "
        "which attaches the internal token itself -- so forwarding it means "
        "signing an anonymous request for a list of other people's payments")


@pytest.mark.parametrize("path", _PATHS)
def test_a_borrower_is_refused(client, upstream, monkeypatch, path):
    """Authenticated is not authorized. The list names other borrowers."""
    _session(monkeypatch, "borrower")

    resp = client.get(path, headers={"Authorization": "Bearer t"})

    assert resp.status_code == 403
    assert upstream == []


@pytest.mark.parametrize("path", _PATHS)
@pytest.mark.parametrize("role", ["csr", "underwriter", "admin"])
def test_staff_may_read_it(client, upstream, monkeypatch, path, role):
    """CSR included, deliberately.

    `can_move_money` is the gate on charging a card. This reads a list and moves
    nothing, and the CSR fielding the call from the borrower in that list is
    exactly who needs to see it.
    """
    _session(monkeypatch, role)

    resp = client.get(path, headers={"Authorization": "Bearer t"})

    assert resp.status_code == 200
    assert len(upstream) == 1, upstream
    assert upstream[0]["url"].endswith(path)


def test_the_internal_token_is_attached_by_the_gateway(client, upstream, monkeypatch):
    """payment-service refuses anything without it, so the proxy must add it --
    and it must be the gateway's own, never a value the caller supplied."""
    _session(monkeypatch, "admin")

    client.get("/payments/unreconciled/items",
               headers={"Authorization": "Bearer t",
                        "X-Internal-Token": "attacker-supplied"})

    assert len(upstream) == 1
    assert upstream[0]["headers"].get("X-Internal-Token") == main.INTERNAL_SERVICE_TOKEN
    assert upstream[0]["headers"].get("X-Internal-Token") != "attacker-supplied"


def test_nothing_else_under_payments_became_reachable(client, upstream, monkeypatch):
    """The route was opened for two GETs, not for the prefix.

    `POST /payments/reconcile` triggers a real drain pass and stays inside the
    compose network; a path that merely starts with the same words must not be
    proxied because this one is.
    """
    _session(monkeypatch, "admin")

    for path in ("/payments/reconcile", "/payments/unreconciled/items/extra",
                 "/payments/unreconciledX", "/payments/anything"):
        resp = client.get(path, headers={"Authorization": "Bearer t"})
        assert resp.status_code == 404, (path, resp.status_code)

    assert upstream == [], upstream


def test_a_write_to_the_read_route_is_not_proxied(client, upstream, monkeypatch):
    """GET only. The listing is a read, and the route says so by method."""
    _session(monkeypatch, "admin")

    resp = client.post("/payments/unreconciled/items",
                       headers={"Authorization": "Bearer t"}, json={})

    assert resp.status_code == 404
    assert upstream == []
