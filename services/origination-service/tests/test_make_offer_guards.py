"""Tests for POST /offer (services/origination-service/app/routers/offers.py).

Review finding: make_offer had no guard of its own at all -- any decision
outcome (denied, still-pending, no decision yet) proxied straight through to
disclosure-service, which only ever returned a generic "no approved decision
on record" 422 with no reason, and a repeat call for an existing offer
silently returned 200 with the unchanged offer, giving the caller no way to
tell "just created" from "already existed". These tests cover: a specific,
honest message per state (denied-with-reason, no-final-approval,
already-exists), a successful create for an approved application, and
upstream/network failures surfacing a clear message without leaking
internals.
"""
import httpx
import pytest

from app import clients, db
from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)

_OFFER_BODY = {"app_id": 10, "principal": 9000, "term_months": 24}


def test_make_offer_rejects_when_an_offer_already_exists(monkeypatch):
    monkeypatch.setattr(db, "query", lambda sql, params=None: [{"id": 1}])

    resp = client.post("/offer", json=_OFFER_BODY)

    assert resp.status_code == 409
    assert resp.json()["detail"] == "An offer has already been created for this application."


def test_make_offer_rejects_when_no_decision_exists_yet(monkeypatch):
    def _fake_query(sql, params=None):
        if "FROM offers" in sql:
            return []  # no existing offer
        return []  # no decision either

    monkeypatch.setattr(db, "query", _fake_query)

    resp = client.post("/offer", json=_OFFER_BODY)

    assert resp.status_code == 422
    assert resp.json()["detail"] == "An offer cannot be created until the application receives a final approval."


def test_make_offer_rejects_a_still_pending_application(monkeypatch):
    def _fake_query(sql, params=None):
        if "FROM offers" in sql:
            return []
        if "FROM decisions" in sql:
            return [{"outcome": "refer"}]
        return []

    monkeypatch.setattr(db, "query", _fake_query)

    resp = client.post("/offer", json=_OFFER_BODY)

    assert resp.status_code == 422
    assert resp.json()["detail"] == "An offer cannot be created until the application receives a final approval."


def test_make_offer_rejects_a_denied_application_with_its_reason(monkeypatch):
    def _fake_query(sql, params=None):
        if "FROM offers" in sql:
            return []
        if "FROM decisions" in sql:
            return [{"outcome": "deny"}]
        if "FROM manual_reviews" in sql:
            return []  # automated-only deny
        if "FROM decision_events" in sql:
            return [{"reason_codes": ["Low credit bureau score relative to lending criteria"]}]
        return []

    monkeypatch.setattr(db, "query", _fake_query)

    resp = client.post("/offer", json=_OFFER_BODY)

    assert resp.status_code == 422
    assert resp.json()["detail"] == (
        "An offer cannot be created because this application was denied. "
        "Decision reason: Low credit bureau score relative to lending criteria."
    )


def test_make_offer_succeeds_for_an_approved_application(monkeypatch):
    def _fake_query(sql, params=None):
        if "FROM offers" in sql:
            return []
        if "FROM decisions" in sql:
            return [{"outcome": "approve"}]
        return []

    monkeypatch.setattr(db, "query", _fake_query)
    calls = []

    def _fake_post(base_url, path, payload, headers=None):
        calls.append((base_url, path, payload, headers))
        return {
            "disclosure": {
                "apr": 5.2, "finance_charge": 500.0, "monthly_payment": 400.0,
                "amount_financed": 8700.0, "total_of_payments": 9600.0,
            },
            "schedule": [],
        }

    monkeypatch.setattr(clients, "post", _fake_post)

    resp = client.post("/offer", json=_OFFER_BODY)

    assert resp.status_code == 200
    assert resp.json()["disclosure"]["apr"] == 5.2
    assert len(calls) == 1


def test_make_offer_surfaces_disclosure_services_own_message_on_rejection(monkeypatch):
    """A race (outcome changed between our guard check and the call reaching
    disclosure-service) or any other 4xx from disclosure-service itself
    surfaces ITS OWN already user-safe detail message, not a stack trace."""
    def _fake_query(sql, params=None):
        if "FROM offers" in sql:
            return []
        if "FROM decisions" in sql:
            return [{"outcome": "approve"}]
        return []

    monkeypatch.setattr(db, "query", _fake_query)

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
    generic message instead."""
    import logging
    caplog.set_level(logging.ERROR, logger="offers")

    def _fake_query(sql, params=None):
        if "FROM offers" in sql:
            return []
        if "FROM decisions" in sql:
            return [{"outcome": "approve"}]
        return []

    monkeypatch.setattr(db, "query", _fake_query)

    def _fake_post(base_url, path, payload, headers=None):
        raise httpx.ConnectError("connection refused to http://disclosure-service:8005")

    monkeypatch.setattr(clients, "post", _fake_post)

    resp = client.post("/offer", json=_OFFER_BODY)

    assert resp.status_code == 502
    assert resp.json()["detail"] == "Could not create the offer -- please try again."
    assert "disclosure-service" not in resp.text
    assert any("app_id=10" in r.message for r in caplog.records)
