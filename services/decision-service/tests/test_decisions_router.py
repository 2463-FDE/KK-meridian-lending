"""Tests for the POST /decisions router (previously untested end-to-end -- only
decide() itself, called directly with a plain dict, had coverage).

Review finding (gateway security review): the router used to build the
scoring-chain input straight from the request body -- name/ssn/requested_amount/
term_months/annual_income all caller-supplied. Reachable (until the gateway's
own fix) by anyone who could guess an application_id, this let a caller
overwrite a real decision + its audit trail with fabricated financials via
decide()'s ON CONFLICT DO UPDATE. These tests cover the fix: only
application_id is trusted from the caller, everything else is loaded from the
application's own DB record.

Also covers the defense-in-depth X-Internal-Token check added alongside the
docker-compose fix that stopped publishing decision-service's port to the
host -- see docker-compose.yml and app/config.py.
"""
from app import config, db, decision
from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)

_AUTH_HEADERS = {"X-Internal-Token": config.INTERNAL_SERVICE_TOKEN}


def _payload(application_id=10, **overrides):
    body = {
        "application_id": application_id,
        "attempt_id": 1,
        "bureau_request_key": "router-test-key",
        "applicant_id": 5,
        "name": "Attacker Supplied Name",
        "ssn": "000000000",
        "requested_amount": 50000,
        "term_months": 60,
        "annual_income": 1_000_000,
        "monthly_debt": 0,
    }
    body.update(overrides)
    return body


def test_run_decision_loads_application_from_db_ignores_body_financials(monkeypatch):
    """The exact review scenario: caller supplies a fabricated ssn/income/amount/
    term for a real application_id. The application's own DB record (income=40000,
    ssn ending in an odd digit, amount=9000, term=24) must be what actually reaches
    decide() -- never the body's fabricated values."""
    db_rows = [{"id": 10, "amount": 9000, "term_months": 24, "income": 40000, "ssn": "123456781"}]
    monkeypatch.setattr(db, "query", lambda sql, params=None: db_rows)

    captured = {}

    async def _fake_decide(application):
        captured.update(application)
        return {"decision": "refer", "score": 610, "reason_codes": [],
                "bureau_score": 610, "model_version": "v1-stub", "top_features": None}

    monkeypatch.setattr(decision, "decide", _fake_decide)

    resp = client.post(
        "/decisions", json=_payload(application_id=10, attempt_id=42), headers=_AUTH_HEADERS
    )

    assert resp.status_code == 200
    assert captured["app_id"] == 10
    assert captured["ssn"] == "123456781"
    assert captured["income"] == 40000
    assert captured["requested_amount"] == 9000
    assert captured["term_months"] == 24
    # PR #6 review (Finding 2): attempt_id is an opaque correlation id --
    # decision-service must echo it back unchanged, never derive its own.
    assert resp.json()["attempt_id"] == 42


def test_run_decision_404s_when_application_not_found(monkeypatch):
    monkeypatch.setattr(db, "query", lambda sql, params=None: [])

    resp = client.post("/decisions", json=_payload(application_id=999999), headers=_AUTH_HEADERS)

    assert resp.status_code == 404


def test_run_decision_rejects_missing_internal_token(monkeypatch):
    monkeypatch.setattr(db, "query", lambda sql, params=None: [{"id": 10, "amount": 9000, "term_months": 24, "income": 40000, "ssn": "123456781"}])

    resp = client.post("/decisions", json=_payload(application_id=10))

    assert resp.status_code == 401


def test_run_decision_rejects_wrong_internal_token(monkeypatch):
    monkeypatch.setattr(db, "query", lambda sql, params=None: [{"id": 10, "amount": 9000, "term_months": 24, "income": 40000, "ssn": "123456781"}])

    resp = client.post(
        "/decisions", json=_payload(application_id=10),
        headers={"X-Internal-Token": "attacker-guessed-token"},
    )

    assert resp.status_code == 401


def test_run_decision_rejects_everything_when_config_token_unset(monkeypatch):
    """A deploy that forgets to set INTERNAL_SERVICE_TOKEN must fail closed --
    no caller (not even one that sends the empty string) should ever match."""
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "")
    monkeypatch.setattr(db, "query", lambda sql, params=None: [{"id": 10, "amount": 9000, "term_months": 24, "income": 40000, "ssn": "123456781"}])

    resp = client.post("/decisions", json=_payload(application_id=10), headers={"X-Internal-Token": ""})

    assert resp.status_code == 401
