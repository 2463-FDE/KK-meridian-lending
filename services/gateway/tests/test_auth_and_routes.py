"""Tests for the gateway's auth endpoints and proxy authz gates.

gateway had only test_proxy_security.py before (one spoofed-header regression
test), nothing covering /auth/* or the authz gates across /los, /lss, /payments,
/assistant. Review finding: /lss and /payments used to accept ANY authenticated
caller with no role/ownership check at all -- a borrower session could list the
whole loan portfolio, read another borrower's balance/payment history, or call
money-moving actions (adjust-balance, waive-fee) on any loan. These tests cover
the fix: staff-only for portfolio-wide/money-moving actions, owner-or-staff for
a specific loan's read actions and charging a payment, 403/404 otherwise.
"""
import json
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient

from app import auth, main
from app.main import app

client = TestClient(app)

_BORROWER = {"id": 1, "username": "maria", "role": "borrower", "name": "Maria Gonzalez", "applicant_id": 1}
_BORROWER_NO_APPLICANT = {"id": 1, "username": "maria", "role": "borrower", "name": "Maria Gonzalez", "applicant_id": None}


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


# --- GET /lss/loans (full portfolio list) -- staff-only; borrower gets a
# separately-built, ownership-scoped list instead of the raw proxy. ----------

def test_lss_loans_list_staff_proxies_full_portfolio(monkeypatch):
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(auth, "get_session", lambda token: {
        "id": 2, "username": "underwriter", "role": "underwriter", "name": "Sam", "applicant_id": None,
    })

    resp = client.get("/lss/loans", headers={"Authorization": "Bearer faketoken123"})

    assert resp.status_code == 200
    # Proxied to servicing-service, not gateway-built.
    assert "loans" in _FakeAsyncClient.last_url


def test_lss_loans_list_borrower_gets_own_scoped_results(monkeypatch):
    class _FakeDb:
        def query(self, sql, params=None):
            assert params == (1,)
            return [{
                "id": 5, "applicant_name": "Maria Gonzalez", "principal": 10000.0,
                "apr": 12.5, "term_months": 36, "status": "current",
                "balance": 9000.0, "past_due": 0.0, "opened_at": None,
            }]

    monkeypatch.setattr(main, "db", _FakeDb())
    monkeypatch.setattr(auth, "get_session", lambda token: _BORROWER)

    resp = client.get("/lss/loans", headers={"Authorization": "Bearer faketoken123"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["id"] == 5


def test_lss_loans_list_borrower_decimal_rows_serialize(monkeypatch):
    """Review finding: after the D12 NUMERIC migration, raw psycopg2 reads of
    principal/apr/balance/past_due come back as Decimal, not float -- and
    JSONResponse (stdlib json.dumps under the hood) can't serialize Decimal.
    Feeds _borrower_loans() real Decimal values, the way a live NUMERIC column
    actually would, and asserts the route still returns 200 with plain floats."""
    class _FakeDb:
        def query(self, sql, params=None):
            assert params == (1,)
            return [{
                "id": 5, "applicant_name": "Maria Gonzalez",
                "principal": Decimal("10000.00"), "apr": Decimal("12.500"),
                "term_months": 36, "status": "current",
                "balance": Decimal("9000.00"), "past_due": Decimal("0.00"),
                "opened_at": None,
            }]

    monkeypatch.setattr(main, "db", _FakeDb())
    monkeypatch.setattr(auth, "get_session", lambda token: _BORROWER)

    resp = client.get("/lss/loans", headers={"Authorization": "Bearer faketoken123"})

    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["principal"] == 10000.0
    assert item["balance"] == 9000.0


def test_lss_loans_list_borrower_without_applicant_id_is_forbidden(monkeypatch):
    monkeypatch.setattr(auth, "get_session", lambda token: _BORROWER_NO_APPLICANT)

    resp = client.get("/lss/loans", headers={"Authorization": "Bearer faketoken123"})

    assert resp.status_code == 403


# --- GET /lss/loans/{id}(/schedule|/payments) -- owner-or-staff -------------

def test_lss_loan_detail_owner_borrower_is_allowed(monkeypatch):
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(auth, "get_session", lambda token: _BORROWER)
    monkeypatch.setattr(auth, "owns_loan", lambda user, loan_id: True)

    resp = client.get("/lss/loans/5", headers={"Authorization": "Bearer faketoken123"})

    assert resp.status_code == 200


def test_lss_loan_detail_non_owner_borrower_is_forbidden(monkeypatch):
    monkeypatch.setattr(auth, "get_session", lambda token: _BORROWER)
    monkeypatch.setattr(auth, "owns_loan", lambda user, loan_id: False)

    resp = client.get("/lss/loans/999", headers={"Authorization": "Bearer faketoken123"})

    assert resp.status_code == 403


@pytest.mark.parametrize("role", ["csr", "underwriter", "admin"])
def test_lss_loan_detail_staff_bypasses_ownership_check(monkeypatch, role):
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(auth, "get_session", lambda token: {
        "id": 2, "username": "x", "role": role, "name": "X", "applicant_id": None,
    })
    monkeypatch.setattr(auth, "owns_loan", lambda user, loan_id: False)  # must not matter

    resp = client.get("/lss/loans/999", headers={"Authorization": "Bearer faketoken123"})

    assert resp.status_code == 200


@pytest.mark.parametrize("suffix", ["schedule", "payments"])
def test_lss_loan_subresource_owner_or_staff(monkeypatch, suffix):
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(auth, "get_session", lambda token: _BORROWER)
    monkeypatch.setattr(auth, "owns_loan", lambda user, loan_id: True)

    resp = client.get(f"/lss/loans/5/{suffix}", headers={"Authorization": "Bearer faketoken123"})

    assert resp.status_code == 200


# --- /lss/accounts/{id}/* -- balance is owner-or-staff (read-only); the
# money-moving actions are staff-only regardless of ownership. --------------

def test_lss_account_balance_owner_borrower_is_allowed(monkeypatch):
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(auth, "get_session", lambda token: _BORROWER)
    monkeypatch.setattr(auth, "owns_loan", lambda user, loan_id: True)

    resp = client.get("/lss/accounts/5/balance", headers={"Authorization": "Bearer faketoken123"})

    assert resp.status_code == 200


def test_lss_account_balance_non_owner_borrower_is_forbidden(monkeypatch):
    monkeypatch.setattr(auth, "get_session", lambda token: _BORROWER)
    monkeypatch.setattr(auth, "owns_loan", lambda user, loan_id: False)

    resp = client.get("/lss/accounts/999/balance", headers={"Authorization": "Bearer faketoken123"})

    assert resp.status_code == 403


@pytest.mark.parametrize("action", ["adjust-balance", "waive-fee", "late-fee"])
def test_lss_account_money_moving_action_rejects_owning_borrower(monkeypatch, action):
    # Review finding: this used to be reachable by ANY authenticated user,
    # including the loan's own borrower. Owning the loan is not enough for a
    # money-moving action -- these are staff-only, full stop.
    monkeypatch.setattr(auth, "get_session", lambda token: _BORROWER)
    monkeypatch.setattr(auth, "owns_loan", lambda user, loan_id: True)

    resp = client.post(
        f"/lss/accounts/5/{action}", json={"new_balance": 0, "amount": 0},
        headers={"Authorization": "Bearer faketoken123"},
    )

    assert resp.status_code == 403


@pytest.mark.parametrize("action", ["adjust-balance", "waive-fee", "late-fee"])
def test_lss_account_money_moving_action_allows_staff(monkeypatch, action):
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(auth, "get_session", lambda token: {
        "id": 2, "username": "x", "role": "admin", "name": "X", "applicant_id": None,
    })

    resp = client.post(
        f"/lss/accounts/5/{action}", json={"new_balance": 0, "amount": 0},
        headers={"Authorization": "Bearer faketoken123"},
    )

    assert resp.status_code == 200


def test_lss_reconciliation_is_staff_only(monkeypatch):
    monkeypatch.setattr(auth, "get_session", lambda token: _BORROWER)

    resp = client.get("/lss/reconciliation/peek", headers={"Authorization": "Bearer faketoken123"})

    assert resp.status_code == 403


def test_lss_unrecognized_subpath_fails_closed_not_found(monkeypatch):
    # No authz rule accounts for this shape -- must 404, never silently proxy.
    monkeypatch.setattr(auth, "get_session", lambda token: {
        "id": 2, "username": "x", "role": "admin", "name": "X", "applicant_id": None,
    })

    resp = client.get("/lss/accounts/5/apply-payment", headers={"Authorization": "Bearer faketoken123"})

    assert resp.status_code == 404


# --- POST /payments -- staff can charge any loan; a borrower only their own. -

def test_payments_requires_authentication(monkeypatch):
    monkeypatch.setattr(auth, "get_session", lambda token: None)

    resp = client.post("/payments", json={"loan_id": 1, "amount": 10})

    assert resp.status_code == 401


def test_payments_staff_can_charge_any_loan(monkeypatch):
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(auth, "get_session", lambda token: {
        "id": 2, "username": "x", "role": "csr", "name": "X", "applicant_id": None,
    })

    resp = client.post(
        "/payments", json={"loan_id": 999, "amount": 50},
        headers={"Authorization": "Bearer faketoken123"},
    )

    assert resp.status_code == 200
    # Pre-existing bug fixed in passing: this used to proxy to payment-service's
    # bare "/" (404 for everyone) instead of its actual POST /payments route.
    assert _FakeAsyncClient.last_url.endswith("/payments")


def test_payments_borrower_can_charge_own_loan(monkeypatch):
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(auth, "get_session", lambda token: _BORROWER)
    monkeypatch.setattr(auth, "owns_loan", lambda user, loan_id: True)

    resp = client.post(
        "/payments", json={"loan_id": 5, "amount": 50},
        headers={"Authorization": "Bearer faketoken123"},
    )

    assert resp.status_code == 200
    assert _FakeAsyncClient.last_url.endswith("/payments")


def test_payments_borrower_cannot_charge_other_loan(monkeypatch):
    # This is the exact break the review flagged: a borrower must not be able
    # to trigger a charge applied to someone else's loan.
    monkeypatch.setattr(auth, "get_session", lambda token: _BORROWER)
    monkeypatch.setattr(auth, "owns_loan", lambda user, loan_id: False)

    resp = client.post(
        "/payments", json={"loan_id": 999, "amount": 50},
        headers={"Authorization": "Bearer faketoken123"},
    )

    assert resp.status_code == 403


def test_payments_unrecognized_subpath_fails_closed_not_found(monkeypatch):
    monkeypatch.setattr(auth, "get_session", lambda token: {
        "id": 2, "username": "x", "role": "admin", "name": "X", "applicant_id": None,
    })

    resp = client.get("/payments/some-other-path", headers={"Authorization": "Bearer faketoken123"})

    assert resp.status_code == 404


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


def test_decision_requires_authentication(monkeypatch):
    # Security fix: this route used to proxy with an optional session -- an
    # anonymous caller could POST /decision/decisions directly with an SSN,
    # triggering a real credit pull and overwriting the decision for any
    # existing application via the upsert.
    monkeypatch.setattr(auth, "get_session", lambda token: None)

    resp = client.post("/decision/decisions", json={"application_id": 1})

    assert resp.status_code == 401


def test_decision_rejects_non_staff_role(monkeypatch):
    monkeypatch.setattr(auth, "get_session", lambda token: {
        "id": 1, "username": "maria", "role": "borrower", "name": "Maria Gonzalez",
    })

    resp = client.post(
        "/decision/decisions", json={"application_id": 1},
        headers={"Authorization": "Bearer faketoken123"},
    )

    assert resp.status_code == 403


@pytest.mark.parametrize("role", ["csr", "underwriter", "admin"])
def test_decision_accepts_staff_roles(monkeypatch, role):
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(auth, "get_session", lambda token: {
        "id": 2, "username": "x", "role": role, "name": "X",
    })

    resp = client.post(
        "/decision/decisions", json={"application_id": 1},
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
