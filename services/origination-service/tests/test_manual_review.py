"""Tests for POST /applications/{app_id}/review (db/migrations/0018).

Feature: a staff tool to resolve a "refer" decision (policies/underwriting_
guidelines.md's manual-review band, score 600-659 or DTI 43-50%) into a real
approve/deny. Before this, accept_offer already correctly blocked self-accept
on anything but "approve", but nothing existed to move a refer OUT of that
state at all -- these tests cover the new endpoint: staff-only, only usable
on an actual "refer" outcome, and an approve gets the same auto-offer +
accept_token treatment the automated approve path in run_decision gets.
"""
from app import config, db
from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)

_STAFF_HEADERS = {"X-User-Role": "underwriter", "X-Internal-Token": config.INTERNAL_SERVICE_TOKEN}


def _fake_query(decision_outcome, calls=None):
    """Mirrors test_decision_and_accept_authz.py's substring-routed fake db."""

    def _query(sql, params=None):
        if calls is not None:
            calls.append((sql, params))
        stmt = sql.strip()
        if stmt.startswith("SELECT id FROM applications"):
            return [{"id": 10}]
        if "FROM decisions" in stmt:
            return [{"outcome": decision_outcome}] if decision_outcome else []
        return []

    return _query


def test_review_requires_a_staff_session(monkeypatch):
    """No X-Internal-Token (or an anonymous/borrower call) must never be able
    to resolve a refer -- this isn't a decision the applicant can make for
    themselves."""
    monkeypatch.setattr(db, "query", _fake_query("refer"))

    resp = client.post("/applications/10/review", json={"outcome": "approve", "reason": "manual ok"})

    assert resp.status_code == 403


def test_review_requires_a_role_in_staff_roles(monkeypatch):
    monkeypatch.setattr(db, "query", _fake_query("refer"))

    resp = client.post(
        "/applications/10/review",
        json={"outcome": "approve", "reason": "manual ok"},
        headers={"X-User-Role": "underwriter"},  # no X-Internal-Token
    )

    assert resp.status_code == 403


def test_review_404s_on_a_nonexistent_application(monkeypatch):
    def _query(sql, params=None):
        if sql.strip().startswith("SELECT id FROM applications"):
            return []
        return []

    monkeypatch.setattr(db, "query", _query)

    resp = client.post(
        "/applications/999/review",
        json={"outcome": "approve", "reason": "manual ok"},
        headers=_STAFF_HEADERS,
    )

    assert resp.status_code == 404


def test_review_422s_when_no_decision_exists_yet(monkeypatch):
    monkeypatch.setattr(db, "query", _fake_query(None))

    resp = client.post(
        "/applications/10/review",
        json={"outcome": "approve", "reason": "manual ok"},
        headers=_STAFF_HEADERS,
    )

    assert resp.status_code == 422


def test_review_422s_on_an_already_decided_approve(monkeypatch):
    """Only a genuine 'refer' can be manually reviewed -- an already-approved
    or already-denied application isn't up for staff override via this
    endpoint (and a second review of an already-reviewed refer hits this same
    guard, since its outcome is no longer 'refer')."""
    monkeypatch.setattr(db, "query", _fake_query("approve"))

    resp = client.post(
        "/applications/10/review",
        json={"outcome": "deny", "reason": "changed my mind"},
        headers=_STAFF_HEADERS,
    )

    assert resp.status_code == 422


def test_review_approve_records_outcome_and_mints_accept_token(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "query", _fake_query("refer", calls))

    resp = client.post(
        "/applications/10/review",
        json={"outcome": "approve", "reason": "DTI recalculated under 43% with updated income"},
        headers=_STAFF_HEADERS,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "approve"
    assert body["accept_token"]
    assert body["adverse_action_reason"] is None

    update_decisions = [c for c in calls if c[0].strip().startswith("UPDATE decisions")]
    assert update_decisions and update_decisions[0][1] == ("approve", 10)

    audit_inserts = [c for c in calls if c[0].strip().startswith("INSERT INTO manual_reviews")]
    assert len(audit_inserts) == 1
    assert audit_inserts[0][1] == (10, "underwriter", "approve", "DTI recalculated under 43% with updated income")

    status_updates = [c for c in calls if "SET status" in c[0]]
    assert status_updates and status_updates[0][1] == ("approved", 10)
    # Review fix parity with run_decision: never regress an already-funded row.
    assert "status <> 'funded'" in status_updates[0][0]


def test_review_deny_returns_the_staff_reason_as_adverse_action(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "query", _fake_query("refer", calls))

    resp = client.post(
        "/applications/10/review",
        json={"outcome": "deny", "reason": "DTI still above policy after re-verification"},
        headers=_STAFF_HEADERS,
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["decision"] == "deny"
    assert body["accept_token"] is None
    assert body["adverse_action_reason"] == "DTI still above policy after re-verification"

    update_decisions = [c for c in calls if c[0].strip().startswith("UPDATE decisions")]
    assert update_decisions and update_decisions[0][1] == ("deny", 10)

    # No accept_token mint, no offer-generation attempt, on a deny.
    accept_token_updates = [c for c in calls if "accept_token" in c[0]]
    assert accept_token_updates == []


def test_review_rejects_an_empty_reason(monkeypatch):
    monkeypatch.setattr(db, "query", _fake_query("refer"))

    resp = client.post(
        "/applications/10/review",
        json={"outcome": "deny", "reason": ""},
        headers=_STAFF_HEADERS,
    )

    assert resp.status_code == 422
