"""Authz tests for POST /applications/{app_id}/decision and /accept.

Review finding: the gateway proxies every /los/* path anonymously
(gateway/app/main.py:138-143 -- an applicant has no account yet, so this is
by design for public submission/status routes), but neither /decision nor
/accept had any role/ownership check of their own -- anyone who guessed an
application id could rerun decisioning on a stranger's application, or
board/fund a real loan for one that was never even approved.

These tests cover the fix: the first decision and the first accept for an
application still run anonymously (the legitimate no-account borrower flow
in frontend/app/apply/page.tsx is unaffected), but a decision RERUN or a
re-ACCEPT of an already-funded application now requires a staff session, and
accept now refuses an application that was never actually approved.
"""
from app import clients, db, intake
from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)

_APPLICATION_ROW = {
    "id": 10, "applicant_id": 5, "amount": 9000, "term_months": 24,
    "income": 40000, "name": "Jane Borrower", "ssn": "123456781",
}


# --- POST /{app_id}/decision -------------------------------------------------

def _fake_decision_client_post(monkeypatch, response=None):
    calls = []

    def _fake_post(base_url, path, payload, headers=None):
        calls.append((base_url, path, payload, headers))
        return response or {"outcome": "approve", "score": 700, "reason": None}

    monkeypatch.setattr(clients, "post", _fake_post)
    return calls


def test_first_decision_for_an_application_runs_anonymously(monkeypatch):
    def _fake_query(sql, params=None):
        if "FROM decisions" in sql:
            return []  # no decision on record yet -- this is the first run
        return [_APPLICATION_ROW]

    monkeypatch.setattr(db, "query", _fake_query)
    _fake_decision_client_post(monkeypatch)

    resp = client.post("/applications/10/decision")

    assert resp.status_code == 200


def test_rerun_of_an_existing_decision_requires_staff(monkeypatch):
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
    def _fake_query(sql, params=None):
        if "FROM decisions" in sql:
            return [{"app_id": 10}]
        return [_APPLICATION_ROW]

    monkeypatch.setattr(db, "query", _fake_query)
    _fake_decision_client_post(monkeypatch)

    resp = client.post("/applications/10/decision", headers={"X-User-Role": "underwriter"})

    assert resp.status_code == 200


# --- POST /{app_id}/accept ---------------------------------------------------

def _accept_row(status="approved", outcome="approve", apr=9.99):
    return {
        "amount": 9000, "term_months": 24, "status": status,
        "name": "Jane Borrower", "apr": apr, "outcome": outcome,
    }


def _stub_board_to_servicing(monkeypatch, loan_id=555):
    calls = []
    monkeypatch.setattr(
        intake, "board_to_servicing",
        lambda *a, **k: calls.append((a, k)) or loan_id,
    )
    return calls


def test_first_accept_of_an_approved_application_runs_anonymously(monkeypatch):
    monkeypatch.setattr(db, "query", lambda sql, params=None: [_accept_row(status="approved")])
    board_calls = _stub_board_to_servicing(monkeypatch)

    resp = client.post("/applications/10/accept")

    assert resp.status_code == 200
    assert resp.json()["loan_id"] == 555
    assert len(board_calls) == 1


def test_accept_rejects_application_that_was_never_approved(monkeypatch):
    """The other half of the review finding: accept never checked the
    decision outcome at all -- a denied or still-pending application could be
    boarded/funded like any other."""
    monkeypatch.setattr(db, "query", lambda sql, params=None: [_accept_row(status="submitted", outcome="deny")])
    board_calls = _stub_board_to_servicing(monkeypatch)

    resp = client.post("/applications/10/accept")

    assert resp.status_code == 422
    assert not board_calls  # never boards/funds a loan for a non-approved application


def test_reaccept_of_an_already_funded_application_requires_staff(monkeypatch):
    monkeypatch.setattr(db, "query", lambda sql, params=None: [_accept_row(status="funded")])
    board_calls = _stub_board_to_servicing(monkeypatch)

    resp = client.post("/applications/10/accept")

    assert resp.status_code == 403
    assert not board_calls  # never re-boards/re-funds


def test_reaccept_of_an_already_funded_application_succeeds_for_staff(monkeypatch):
    monkeypatch.setattr(db, "query", lambda sql, params=None: [_accept_row(status="funded")])
    _stub_board_to_servicing(monkeypatch)

    resp = client.post("/applications/10/accept", headers={"X-User-Role": "underwriter"})

    assert resp.status_code == 200
