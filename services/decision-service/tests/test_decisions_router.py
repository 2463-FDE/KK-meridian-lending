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
"""
from app import db, decision
from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)


def _payload(application_id=10, **overrides):
    body = {
        "application_id": application_id,
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
        return {"decision": "refer", "score": 610, "reason_codes": []}

    monkeypatch.setattr(decision, "decide", _fake_decide)

    resp = client.post("/decisions", json=_payload(application_id=10))

    assert resp.status_code == 200
    assert captured["app_id"] == 10
    assert captured["ssn"] == "123456781"
    assert captured["income"] == 40000
    assert captured["requested_amount"] == 9000
    assert captured["term_months"] == 24


def test_run_decision_404s_when_application_not_found(monkeypatch):
    monkeypatch.setattr(db, "query", lambda sql, params=None: [])

    resp = client.post("/decisions", json=_payload(application_id=999999))

    assert resp.status_code == 404
