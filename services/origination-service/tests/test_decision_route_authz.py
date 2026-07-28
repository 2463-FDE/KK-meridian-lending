"""Authz tests for POST /applications/{app_id}/decision.

Review finding: this route has no session check of its own -- the gateway's
/los/* proxy forwards it anonymously on purpose (a freshly-submitted
applicant has no account yet, and this is how they get their first decision --
see frontend/app/apply/page.tsx's "Get decision" button). Without a check,
though, the same route let anyone who guesses a numeric app_id rerun
decisioning on a STRANGER's already-decided application: a real credit bureau
pull, plus an overwrite of their decision row via decision-service's own
ON CONFLICT (app_id) DO UPDATE (graph.py).

These tests cover the fix: the first decision for an app_id still runs
anonymously (the legitimate borrower flow is unaffected), but once a decision
already exists for that app_id, a rerun requires a staff session -- the same
_STAFF_ROLES gate get_application_financials already uses. The underwriting
console's own "Run decision" button (frontend/app/underwriting/[appId]/page.tsx)
already sends a staff session, so this doesn't change that flow.
"""
from app import clients, db
from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)

_APPLICATION_ROW = {
    "id": 10, "applicant_id": 5, "amount": 9000, "term_months": 24,
    "income": 40000, "name": "Jane Borrower", "ssn": "123456781",
}


def _fake_decision_client_post(monkeypatch, response=None):
    calls = []

    def _fake_post(base_url, path, payload, headers=None):
        calls.append((base_url, path, payload, headers))
        return response or {"outcome": "approve", "score": 700, "reason": None}

    monkeypatch.setattr(clients, "post", _fake_post)
    return calls


def test_first_decision_for_an_application_runs_anonymously(monkeypatch):
    """No account exists yet at this point in the flow -- must not regress the
    borrower's own "Get decision" action on a fresh application."""
    def _fake_query(sql, params=None):
        if "FROM decisions" in sql:
            return []  # no decision on record yet -- this is the first run
        return [_APPLICATION_ROW]

    monkeypatch.setattr(db, "query", _fake_query)
    _fake_decision_client_post(monkeypatch)

    resp = client.post("/applications/10/decision")

    assert resp.status_code == 200


def test_rerun_of_an_existing_decision_requires_staff(monkeypatch):
    """The exact review scenario: a decision already exists for this app_id
    (someone else's real, already-decided application), and an anonymous
    caller who merely guessed the app_id tries to rerun it."""
    def _fake_query(sql, params=None):
        if "FROM decisions" in sql:
            return [{"app_id": 10}]  # a decision already exists
        return [_APPLICATION_ROW]

    monkeypatch.setattr(db, "query", _fake_query)
    calls = _fake_decision_client_post(monkeypatch)

    resp = client.post("/applications/10/decision")

    assert resp.status_code == 403
    assert not calls  # never reached decision-service -- no bureau pull triggered


def test_rerun_of_an_existing_decision_succeeds_for_staff(monkeypatch):
    """The underwriting console's own "Run decision" button -- a staff session
    reruns an already-decided application on purpose."""
    def _fake_query(sql, params=None):
        if "FROM decisions" in sql:
            return [{"app_id": 10}]
        return [_APPLICATION_ROW]

    monkeypatch.setattr(db, "query", _fake_query)
    _fake_decision_client_post(monkeypatch)

    resp = client.post("/applications/10/decision", headers={"X-User-Role": "underwriter"})

    assert resp.status_code == 200
