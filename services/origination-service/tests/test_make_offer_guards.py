"""Tests for POST /offer and GET /applications/{app_id}/offer
(services/origination-service/app/routers/offers.py).

Review finding: make_offer had no guard of its own at all -- any decision
outcome (denied, still-pending, no decision yet) proxied straight through to
disclosure-service, which only ever returned a generic "no approved decision
on record" 422 with no reason. Guards added: a specific, structured
{"code", "message"} conflict per state (denied-with-reason, no-final-
approval, already-boarded), and upstream/network failures surfacing a clear
message without leaking internals.

Bug fix (borrower-workflow audit): make_offer used to ALSO reject with a
generic 409 the instant ANY offer already existed -- including the normal
case where run_decision's/review_application's own best-effort
auto-generation had already created one. This broke the public /apply
page's own "view your offer" step (it always tried to create, always found
one already there, always 409'd). make_offer is now itself idempotent:
disclosure-service's own INSERT ... ON CONFLICT (decision_id) DO NOTHING
already guarantees exactly one offer row per decision (offers.decision_id /
offers.app_id are both UNIQUE) -- make_offer just returns that SAME offer
either way, with `created` telling "just created" from "already existed"
apart. These tests cover that fix, plus GET /applications/{app_id}/offer's
new ownership check (previously had none at all -- app_id is a guessable
sequential integer).
"""
import httpx
import pytest

from app import clients, db, decision_state
from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)

_OFFER_BODY = {"app_id": 10, "principal": 9000, "term_months": 24}

_ACCEPT_TOKEN = "borrower-own-token-xyz"
_ACCEPT_TOKEN_HASH = decision_state.hash_accept_token(_ACCEPT_TOKEN)


def _fake_query_factory(status="approved", outcome="approve", offer_created=False):
    def _fake_query(sql, params=None):
        if "SELECT status FROM applications" in sql:
            return [{"status": status}]
        if "FROM decisions" in sql:
            return [{"outcome": outcome}] if outcome else []
        if "FROM manual_reviews" in sql:
            return []
        if "FROM decision_events" in sql:
            return [{"reason_codes": ["Low credit bureau score relative to lending criteria"]}]
        return []
    return _fake_query


def test_make_offer_returns_the_existing_offer_when_one_already_exists(monkeypatch):
    """The normal case: auto-generation already created this offer the
    instant the decision came back approve. A retried/first browser call
    must get that SAME offer back, not a 409."""
    monkeypatch.setattr(db, "query", _fake_query_factory())
    calls = []

    def _fake_post(base_url, path, payload, headers=None):
        calls.append((base_url, path, payload, headers))
        return {
            "disclosure": {
                "apr": 5.2, "finance_charge": 500.0, "monthly_payment": 400.0,
                "amount_financed": 8700.0, "total_of_payments": 9600.0,
            },
            "schedule": [],
            "created": False,
        }

    monkeypatch.setattr(clients, "post", _fake_post)

    resp = client.post("/offer", json=_OFFER_BODY)

    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] is False
    assert body["disclosure"]["apr"] == 5.2
    assert len(calls) == 1  # still one real call through to disclosure-service


def test_make_offer_rejects_an_already_boarded_application(monkeypatch):
    monkeypatch.setattr(db, "query", _fake_query_factory(status="funded"))

    resp = client.post("/offer", json=_OFFER_BODY)

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["code"] == "APPLICATION_ALREADY_BOARDED"


def test_make_offer_rejects_when_application_not_found(monkeypatch):
    monkeypatch.setattr(db, "query", lambda sql, params=None: [])

    resp = client.post("/offer", json=_OFFER_BODY)

    assert resp.status_code == 404


def test_make_offer_rejects_when_no_decision_exists_yet(monkeypatch):
    monkeypatch.setattr(db, "query", _fake_query_factory(outcome=None))

    resp = client.post("/offer", json=_OFFER_BODY)

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "APPLICATION_NOT_APPROVED"


def test_make_offer_rejects_a_still_pending_application(monkeypatch):
    monkeypatch.setattr(db, "query", _fake_query_factory(outcome="refer"))

    resp = client.post("/offer", json=_OFFER_BODY)

    assert resp.status_code == 422
    assert resp.json()["detail"]["code"] == "APPLICATION_NOT_APPROVED"


def test_make_offer_rejects_a_denied_application_with_its_reason(monkeypatch):
    monkeypatch.setattr(db, "query", _fake_query_factory(outcome="deny"))

    resp = client.post("/offer", json=_OFFER_BODY)

    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["code"] == "APPLICATION_NOT_APPROVED"
    assert "Low credit bureau score relative to lending criteria" in detail["message"]


def test_make_offer_succeeds_for_an_approved_application(monkeypatch):
    monkeypatch.setattr(db, "query", _fake_query_factory())
    calls = []

    def _fake_post(base_url, path, payload, headers=None):
        calls.append((base_url, path, payload, headers))
        return {
            "disclosure": {
                "apr": 5.2, "finance_charge": 500.0, "monthly_payment": 400.0,
                "amount_financed": 8700.0, "total_of_payments": 9600.0,
            },
            "schedule": [],
            "created": True,
        }

    monkeypatch.setattr(clients, "post", _fake_post)

    resp = client.post("/offer", json=_OFFER_BODY)

    assert resp.status_code == 200
    body = resp.json()
    assert body["created"] is True
    assert body["disclosure"]["apr"] == 5.2
    assert len(calls) == 1


def test_make_offer_surfaces_disclosure_services_own_message_on_rejection(monkeypatch):
    """A race (outcome changed between our guard check and the call reaching
    disclosure-service) or any other 4xx from disclosure-service itself
    surfaces ITS OWN already user-safe detail message, not a stack trace."""
    monkeypatch.setattr(db, "query", _fake_query_factory())

    def _fake_post(base_url, path, payload, headers=None):
        request = httpx.Request("POST", f"{base_url}{path}")
        response = httpx.Response(
            422, json={"detail": "no approved decision on record for application_id=10"}, request=request,
        )
        raise httpx.HTTPStatusError("422", request=request, response=response)

    monkeypatch.setattr(clients, "post", _fake_post)

    resp = client.post("/offer", json=_OFFER_BODY)

    assert resp.status_code == 422
    assert resp.json()["detail"] == "no approved decision on record for application_id=10"


def test_make_offer_logs_technical_failure_without_exposing_it(monkeypatch, caplog):
    """A network failure/timeout must not leak internals (host, port,
    exception class) to the caller -- log it server-side, return a clear,
    generic, recoverable message instead."""
    import logging
    caplog.set_level(logging.ERROR, logger="offers")

    monkeypatch.setattr(db, "query", _fake_query_factory())

    def _fake_post(base_url, path, payload, headers=None):
        raise httpx.ConnectError("connection refused to http://disclosure-service:8005")

    monkeypatch.setattr(clients, "post", _fake_post)

    resp = client.post("/offer", json=_OFFER_BODY)

    assert resp.status_code == 502
    detail = resp.json()["detail"]
    assert detail["code"] == "OFFER_SERVICE_UNAVAILABLE"
    assert "disclosure-service" not in resp.text
    assert any("app_id=10" in r.message for r in caplog.records)


# --- GET /applications/{app_id}/offer ---------------------------------------

def _fake_offer_get(status_code=200, body=None):
    def _fake_get(base_url, path):
        import httpx as _httpx
        request = _httpx.Request("GET", f"{base_url}{path}")
        return _httpx.Response(status_code, json=body or {}, request=request)
    return _fake_get


def test_get_offer_rejects_anonymous_caller_with_no_token(monkeypatch):
    monkeypatch.setattr(db, "query", lambda sql, params=None: [{"accept_token_hash": _ACCEPT_TOKEN_HASH}])

    resp = client.get("/applications/10/offer")

    assert resp.status_code == 403


def test_get_offer_rejects_wrong_token(monkeypatch):
    monkeypatch.setattr(db, "query", lambda sql, params=None: [{"accept_token_hash": _ACCEPT_TOKEN_HASH}])

    resp = client.get("/applications/10/offer", params={"accept_token": "attacker-guessed"})

    assert resp.status_code == 403


def test_get_offer_succeeds_with_the_correct_accept_token(monkeypatch):
    monkeypatch.setattr(db, "query", lambda sql, params=None: [{"accept_token_hash": _ACCEPT_TOKEN_HASH}])
    monkeypatch.setattr(
        clients, "get",
        _fake_offer_get(200, {
            "disclosure": {
                "apr": 5.2, "finance_charge": 500.0, "monthly_payment": 400.0,
                "amount_financed": 8700.0, "total_of_payments": 9600.0,
            },
            "schedule": [],
        }),
    )

    resp = client.get("/applications/10/offer", params={"accept_token": _ACCEPT_TOKEN})

    assert resp.status_code == 200
    assert resp.json()["disclosure"]["apr"] == 5.2


def test_get_offer_succeeds_for_staff_without_a_token(monkeypatch):
    from app import config

    monkeypatch.setattr(db, "query", lambda sql, params=None: [{"accept_token_hash": _ACCEPT_TOKEN_HASH}])
    monkeypatch.setattr(
        clients, "get",
        _fake_offer_get(200, {
            "disclosure": {
                "apr": 5.2, "finance_charge": 500.0, "monthly_payment": 400.0,
                "amount_financed": 8700.0, "total_of_payments": 9600.0,
            },
            "schedule": [],
        }),
    )

    resp = client.get(
        "/applications/10/offer",
        headers={"X-User-Role": "underwriter", "X-Internal-Token": config.INTERNAL_SERVICE_TOKEN},
    )

    assert resp.status_code == 200


def test_get_offer_rejects_staff_role_without_internal_token(monkeypatch):
    monkeypatch.setattr(db, "query", lambda sql, params=None: [{"accept_token_hash": _ACCEPT_TOKEN_HASH}])

    resp = client.get("/applications/10/offer", headers={"X-User-Role": "underwriter"})

    assert resp.status_code == 403


def test_get_offer_returns_404_when_no_offer_exists(monkeypatch):
    monkeypatch.setattr(db, "query", lambda sql, params=None: [{"accept_token_hash": _ACCEPT_TOKEN_HASH}])
    monkeypatch.setattr(clients, "get", _fake_offer_get(404, {"detail": "no offer for this application"}))

    resp = client.get("/applications/10/offer", params={"accept_token": _ACCEPT_TOKEN})

    assert resp.status_code == 404


def test_get_offer_never_exposes_the_stored_token_hash(monkeypatch):
    monkeypatch.setattr(db, "query", lambda sql, params=None: [{"accept_token_hash": _ACCEPT_TOKEN_HASH}])
    monkeypatch.setattr(
        clients, "get",
        _fake_offer_get(200, {
            "disclosure": {
                "apr": 5.2, "finance_charge": 500.0, "monthly_payment": 400.0,
                "amount_financed": 8700.0, "total_of_payments": 9600.0,
            },
            "schedule": [],
        }),
    )

    resp = client.get("/applications/10/offer", params={"accept_token": _ACCEPT_TOKEN})

    assert resp.status_code == 200
    assert _ACCEPT_TOKEN_HASH not in resp.text
    assert "accept_token_hash" not in resp.text
