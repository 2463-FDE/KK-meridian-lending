"""Tests for the POST /decisions router's X-Internal-Token check.

Defense-in-depth added alongside the docker-compose fix that stopped
publishing decision-service's port to the host -- see docker-compose.yml and
app/config.py. The network boundary is the primary control; this is the
fallback in case that boundary is ever mistakenly reopened.
"""
from app import config, decision
from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)

_AUTH_HEADERS = {"X-Internal-Token": config.INTERNAL_SERVICE_TOKEN}


def _payload(application_id=10, **overrides):
    body = {
        "application_id": application_id,
        "applicant_id": 5,
        "name": "Jane Borrower",
        "ssn": "123456781",
        "requested_amount": 9000,
        "term_months": 24,
        "annual_income": 40000,
        "monthly_debt": 0,
    }
    body.update(overrides)
    return body


def _fake_decide_ok(monkeypatch):
    async def _fake(application):
        return {"decision": "refer", "score": 610, "reason_codes": []}
    monkeypatch.setattr(decision, "decide", _fake)


def test_run_decision_accepts_correct_internal_token(monkeypatch):
    _fake_decide_ok(monkeypatch)

    resp = client.post("/decisions", json=_payload(), headers=_AUTH_HEADERS)

    assert resp.status_code == 200


def test_run_decision_rejects_missing_internal_token(monkeypatch):
    _fake_decide_ok(monkeypatch)

    resp = client.post("/decisions", json=_payload())

    assert resp.status_code == 401


def test_run_decision_rejects_wrong_internal_token(monkeypatch):
    _fake_decide_ok(monkeypatch)

    resp = client.post(
        "/decisions", json=_payload(),
        headers={"X-Internal-Token": "attacker-guessed-token"},
    )

    assert resp.status_code == 401


def test_run_decision_rejects_everything_when_config_token_unset(monkeypatch):
    """A deploy that forgets to set INTERNAL_SERVICE_TOKEN must fail closed --
    no caller (not even one that sends the empty string) should ever match."""
    monkeypatch.setattr(config, "INTERNAL_SERVICE_TOKEN", "")
    _fake_decide_ok(monkeypatch)

    resp = client.post("/decisions", json=_payload(), headers={"X-Internal-Token": ""})

    assert resp.status_code == 401
