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
        # _proxy decodes resp.content directly (UTF-8 mojibake fix) rather
        # than calling resp.json()/resp.text -- mirror a real httpx.Response.
        self.content = self.text.encode("utf-8")

    def json(self):
        return self._json_body


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient so proxy tests never need a live
    downstream service -- records the request it received and returns a fixed
    200 body, mirroring test_proxy_security.py's existing fake. Tests that
    need a specific response body (e.g. the non-ASCII mojibake regression
    test) can set next_response beforehand; it's reset after each request so
    it never leaks into an unrelated test."""

    last_url = None
    last_headers = None
    last_params = None
    next_response = None

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def request(self, method, url, content=None, headers=None, params=None):
        _FakeAsyncClient.last_url = url
        _FakeAsyncClient.last_headers = headers
        _FakeAsyncClient.last_params = params
        if _FakeAsyncClient.next_response is not None:
            resp, _FakeAsyncClient.next_response = _FakeAsyncClient.next_response, None
            return resp
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


def test_los_proxy_forwards_internal_token(monkeypatch):
    # Review fix: origination-service's own staff-gated routes now verify
    # X-Internal-Token in addition to X-User-Role -- the gateway has to
    # actually forward it on every /los/* proxy or every staff action there
    # would break (403 for a real staff session, not just a spoofed one).
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)

    resp = client.get("/los/applications/1")

    assert resp.status_code == 200
    assert _FakeAsyncClient.last_headers["X-Internal-Token"] == main.INTERNAL_SERVICE_TOKEN


def test_los_proxy_forwards_the_offer_accept_token_header(monkeypatch):
    """Security fix (borrower-workflow audit): the offer-view/accept
    credential travels only as X-Offer-Accept-Token now -- the gateway must
    actually forward it (it's just another non-X-User-* inbound header,
    not stripped or specially handled) or origination-service's own
    ownership check would 403 every real borrower request."""
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)

    resp = client.get(
        "/los/applications/1/offer",
        headers={"X-Offer-Accept-Token": "a-real-borrower-token-value"},
    )

    assert resp.status_code == 200
    # HTTP headers are case-insensitive -- ASGI delivers them lowercased
    # regardless of how the client sent them.
    forwarded = {k.lower(): v for k, v in _FakeAsyncClient.last_headers.items()}
    assert forwarded["x-offer-accept-token"] == "a-real-borrower-token-value"


def test_los_proxy_never_puts_the_offer_accept_token_in_the_outbound_url_or_params(monkeypatch):
    """Security fix (follow-up audit): a canary token sent as a header must
    never end up serialized into the outbound request line/query string --
    that was the exact mechanism that leaked a prior version of this same
    credential into this gateway's own access + outbound httpx logs. The
    fake client records `params` (query string) and `url` (path only, no
    query appended by httpx.AsyncClient.request when params is passed
    separately) independently of `headers` -- this proves the header value
    never crosses into either."""
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)
    canary = "CANARY_HEADER_ONLY_VALUE_should_never_appear_in_url_or_params"

    resp = client.get(
        "/los/applications/1/offer",
        headers={"X-Offer-Accept-Token": canary},
    )

    assert resp.status_code == 200
    assert canary not in _FakeAsyncClient.last_url
    assert canary not in str(_FakeAsyncClient.last_params or "")
    forwarded = {k.lower(): v for k, v in _FakeAsyncClient.last_headers.items()}
    assert forwarded["x-offer-accept-token"] == canary


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
                "note_rate_pct": 12.5, "term_months": 36, "status": "current",
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
                "principal": Decimal("10000.00"), "note_rate_pct": Decimal("12.500"),
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


def _borrower_row(**overrides):
    row = {
        "id": 5, "applicant_name": "Maria Gonzalez",
        "principal": Decimal("10000.00"), "note_rate_pct": Decimal("12.500"),
        "schedule_version": "B1", "term_months": 36, "status": "current",
        "balance": Decimal("9000.00"), "past_due": Decimal("0.00"),
        "opened_at": None,
    }
    row.update(overrides)
    return row


def _borrower_list(monkeypatch, row):
    class _FakeDb:
        def query(self, sql, params=None):
            return [row]

    monkeypatch.setattr(main, "db", _FakeDb())
    monkeypatch.setattr(auth, "get_session", lambda token: _BORROWER)
    resp = client.get("/lss/loans", headers={"Authorization": "Bearer faketoken123"})
    assert resp.status_code == 200
    return resp.json()["items"][0]


def test_lss_loans_list_borrower_reports_a_proven_note_rate(monkeypatch):
    """The rate the borrower sees is the contractual one, from a column that can
    only hold that figure since the D19 contract step (db/migrations/0039)."""
    item = _borrower_list(monkeypatch, _borrower_row())
    assert item["note_rate_pct"] == 12.5
    assert item["note_rate_proven"] is True


def test_lss_loans_list_borrower_reports_a_rate_without_a_schedule_version(monkeypatch):
    """This test asserted the OPPOSITE until the contract step, and the history
    is the point.

    `loans.apr` held two different regulated figures: the pre-change path copied
    `offers.apr` -- the DISCLOSED APR, 5.196% for a contract priced at 7.99% --
    into the column servicing amortizes. So this route reported a rate only when
    `schedule_version` proved the current boarding path had written a
    contractual one, and withheld it otherwise. Reviewed on PR #10.

    0038 moved that inference into the data and 0039 dropped `apr`, making
    `note_rate_pct` NOT NULL. `schedule_version` no longer says anything about
    WHICH figure is stored, so withholding on it would now hide a number the
    borrower is entitled to see, for no reason a reader could reconstruct.
    """
    item = _borrower_list(
        monkeypatch,
        _borrower_row(note_rate_pct=Decimal("7.990"), schedule_version=None),
    )
    assert item["note_rate_pct"] == 7.99
    assert item["note_rate_proven"] is True


def test_lss_loans_list_never_publishes_a_field_called_apr(monkeypatch):
    """The name was the defect, so it must not survive in the response either.

    A borrower-facing field called `apr` carrying a note rate is the same
    conflation D19 exists to end, one layer up from the database -- and this is
    the layer the borrower actually reads.
    """
    item = _borrower_list(monkeypatch, _borrower_row())
    assert "apr" not in item, f"the retired name is still published: {sorted(item)}"


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


@pytest.mark.parametrize("action", ["late-fee"])
def test_lss_direct_money_action_rejects_underwriter(monkeypatch, action):
    """`late-fee` still moves money on one person's say-so, so it stays csr/admin.

    Review finding: underwriter is staff (auth.is_staff -> True) but has no
    business moving money alone -- the servicing UI only shows this button to
    CSR/admin, but the gateway used to accept any is_staff() caller, so an
    underwriter could POST straight past the UI and alter a balance.

    `adjust-balance` and `waive-fee` LEFT this list with the maker-checker
    cutover, and the next test is why: they now raise proposals that move
    nothing, and an underwriter is the role that does most of the approving.
    Keeping them here would have blocked the control's main reviewer from using
    the control.
    """
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(auth, "get_session", lambda token: {
        "id": 2, "username": "sam", "role": "underwriter", "name": "Sam Okafor", "applicant_id": None,
    })

    resp = client.post(
        f"/lss/accounts/5/{action}", json={"new_balance": 0, "amount": 0},
        headers={"Authorization": "Bearer faketoken123"},
    )

    assert resp.status_code == 403


@pytest.mark.parametrize("action", ["adjust-balance", "waive-fee", "late-fee"])
@pytest.mark.parametrize("role", ["csr", "admin"])
def test_lss_account_money_moving_action_allows_csr_and_admin(monkeypatch, action, role):
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(auth, "get_session", lambda token: {
        "id": 2, "username": "x", "role": role, "name": "X", "applicant_id": None,
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


def test_lss_review_queue_is_staff_only(monkeypatch):
    """A borrower must not reach the review queue: it lists amounts and capture
    times for loans that are not theirs."""
    monkeypatch.setattr(auth, "get_session", lambda token: _BORROWER)

    resp = client.get("/lss/reconciliation/review-queue",
                      headers={"Authorization": "Bearer faketoken123"})

    assert resp.status_code == 403


def test_lss_review_disposition_is_staff_only(monkeypatch):
    monkeypatch.setattr(auth, "get_session", lambda token: _BORROWER)

    resp = client.post("/lss/reconciliation/review-queue/1/disposition",
                       json={"disposition": "confirmed_duplicate"},
                       headers={"Authorization": "Bearer faketoken123"})

    assert resp.status_code == 403


def test_a_non_numeric_review_item_id_is_not_proxied(monkeypatch):
    """The disposition pattern is anchored and numeric. A permissive one here
    decides which paths reach servicing at all, and the fall-through below is a
    404 by design -- so a path that does not match must land there rather than
    being forwarded and refused somewhere less predictable."""
    monkeypatch.setattr(auth, "get_session", lambda token: {
        "id": 2, "username": "x", "role": "admin", "name": "X", "applicant_id": None,
    })

    resp = client.post("/lss/reconciliation/review-queue/all/disposition",
                       json={"disposition": "confirmed_duplicate"},
                       headers={"Authorization": "Bearer faketoken123"})

    assert resp.status_code == 404


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


# --- POST /payments -- amount validation, both staff and borrower callers. --

@pytest.mark.parametrize("amount", [0, -500, -0.01])
def test_payments_rejects_non_positive_amount_for_staff(monkeypatch, amount):
    # Review finding: a negative amount credited the borrower's balance
    # instead of charging them (servicing computes new_balance = current -
    # amount) -- the gateway is the first hop for staff and borrower alike.
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(auth, "get_session", lambda token: {
        "id": 2, "username": "x", "role": "csr", "name": "X", "applicant_id": None,
    })

    resp = client.post(
        "/payments", json={"loan_id": 999, "amount": amount},
        headers={"Authorization": "Bearer faketoken123"},
    )

    assert resp.status_code == 400


def test_payments_rejects_non_positive_amount_for_borrower(monkeypatch):
    monkeypatch.setattr(auth, "get_session", lambda token: _BORROWER)
    monkeypatch.setattr(auth, "owns_loan", lambda user, loan_id: True)

    resp = client.post(
        "/payments", json={"loan_id": 5, "amount": -500},
        headers={"Authorization": "Bearer faketoken123"},
    )

    assert resp.status_code == 400


def test_payments_rejects_amount_over_the_ceiling(monkeypatch):
    monkeypatch.setattr(auth, "get_session", lambda token: {
        "id": 2, "username": "x", "role": "csr", "name": "X", "applicant_id": None,
    })

    resp = client.post(
        "/payments", json={"loan_id": 999, "amount": 1_000_000.01},
        headers={"Authorization": "Bearer faketoken123"},
    )

    assert resp.status_code == 400


def test_payments_rejects_amount_missing_or_wrong_type(monkeypatch):
    monkeypatch.setattr(auth, "get_session", lambda token: {
        "id": 2, "username": "x", "role": "csr", "name": "X", "applicant_id": None,
    })

    resp = client.post(
        "/payments", json={"loan_id": 999, "amount": "50"},
        headers={"Authorization": "Bearer faketoken123"},
    )

    assert resp.status_code == 400


def test_payments_unrecognized_subpath_fails_closed_not_found(monkeypatch):
    monkeypatch.setattr(auth, "get_session", lambda token: {
        "id": 2, "username": "x", "role": "admin", "name": "X", "applicant_id": None,
    })

    resp = client.get("/payments/some-other-path", headers={"Authorization": "Bearer faketoken123"})

    assert resp.status_code == 404


def test_assistant_summary_requires_authentication(monkeypatch):
    monkeypatch.setattr(auth, "get_session", lambda token: None)

    resp = client.post("/assistant/applications/1/summary")

    assert resp.status_code == 401


def test_assistant_summary_rejects_non_staff_role(monkeypatch):
    monkeypatch.setattr(auth, "get_session", lambda token: {
        "id": 1, "username": "maria", "role": "borrower", "name": "Maria Gonzalez",
    })

    resp = client.post(
        "/assistant/applications/1/summary",
        headers={"Authorization": "Bearer faketoken123"},
    )

    assert resp.status_code == 403


@pytest.mark.parametrize("role", ["csr", "underwriter", "admin"])
def test_assistant_summary_accepts_staff_roles(monkeypatch, role):
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(auth, "get_session", lambda token: {
        "id": 2, "username": "x", "role": role, "name": "X",
    })

    resp = client.post(
        "/assistant/applications/1/summary",
        headers={"Authorization": "Bearer faketoken123"},
    )

    assert resp.status_code == 200


def test_assistant_policy_chat_proxies_anonymously_with_no_session(monkeypatch):
    # Policy Q&A is generic lending-policy content, no per-applicant financials
    # or risk_tier -- unlike /assistant/applications/*/summary, it's open to a
    # borrower with no account, same anonymous-allowed pattern as /los/*.
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(auth, "get_session", lambda token: None)

    resp = client.post("/assistant/policy-chat", json={"question": "x"})

    assert resp.status_code == 200


def test_assistant_policy_chat_allows_borrower_role(monkeypatch):
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(auth, "get_session", lambda token: {
        "id": 1, "username": "maria", "role": "borrower", "name": "Maria Gonzalez",
    })

    resp = client.post(
        "/assistant/policy-chat",
        json={"question": "x"},
        headers={"Authorization": "Bearer faketoken123"},
    )

    assert resp.status_code == 200


@pytest.mark.parametrize("prefix", ["/decision/decisions", "/disclosure/offers"])
def test_decision_and_disclosure_require_authentication(monkeypatch, prefix):
    # Security fix: these used to proxy with an optional session -- an anonymous
    # caller could POST directly to decision-service/disclosure-service (bypassing
    # origination-service entirely) with a guessed application_id and fabricated
    # data, overwriting a real decision or a real approved loan's TILA numbers.
    monkeypatch.setattr(auth, "get_session", lambda token: None)

    resp = client.post(prefix, json={"application_id": 1})

    assert resp.status_code == 401


@pytest.mark.parametrize("prefix", ["/decision/decisions", "/disclosure/offers"])
def test_decision_and_disclosure_reject_non_staff_role(monkeypatch, prefix):
    monkeypatch.setattr(auth, "get_session", lambda token: {
        "id": 1, "username": "maria", "role": "borrower", "name": "Maria Gonzalez",
    })

    resp = client.post(
        prefix, json={"application_id": 1},
        headers={"Authorization": "Bearer faketoken123"},
    )

    assert resp.status_code == 403


@pytest.mark.parametrize("prefix", ["/decision/decisions", "/disclosure/offers"])
@pytest.mark.parametrize("role", ["csr", "underwriter", "admin"])
def test_decision_and_disclosure_accept_staff_roles(monkeypatch, prefix, role):
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(auth, "get_session", lambda token: {
        "id": 2, "username": "x", "role": role, "name": "X",
    })

    resp = client.post(
        prefix, json={"application_id": 1},
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


def test_proxy_does_not_mangle_non_ascii_response_text(monkeypatch):
    """Bug fix: _proxy used to return resp.json() -- httpx decodes via
    resp.text, which falls back to charset auto-detection whenever the
    upstream Content-Type has no explicit charset param (every backend
    service here just sends "application/json" with none). Real live-tested
    finding: an applicant name with an accent or an em dash came back through
    this proxy as visible mojibake ("José" -> "JosÃ©") on every
    proxied route, not just one. JSON is UTF-8 per RFC 8259 -- decode
    resp.content as UTF-8 directly instead of letting httpx guess."""
    monkeypatch.setattr(main.httpx, "AsyncClient", _FakeAsyncClient)
    monkeypatch.setattr(auth, "get_session", lambda token: None)
    _FakeAsyncClient.next_response = _FakeResponse(
        200, {"name": "José Muñoz — Test", "address": "5 Café St"},
    )

    resp = client.get("/los/applications/1")

    assert resp.status_code == 200
    assert resp.json() == {"name": "José Muñoz — Test", "address": "5 Café St"}
