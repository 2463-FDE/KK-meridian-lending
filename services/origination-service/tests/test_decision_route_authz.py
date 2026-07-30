"""Authz tests for POST /applications/{app_id}/decision.

Review finding: this route has no session check of its own -- the gateway's
/los/* proxy forwards it anonymously on purpose (a freshly-submitted
applicant has no account yet, and this is how they get their first decision --
see frontend/app/apply/page.tsx's "Get decision" button). Without a check,
though, the same route let anyone who guesses a numeric app_id trigger the
FIRST decision on a STRANGER's application too -- a real credit bureau pull
using their stored SSN, before a decision even existed yet to gate a rerun on.

These tests cover the fix: the first decision for an app_id requires either a
staff session or the access_token minted onto that application at submission
(see intake.create_application, routers/applications.submit_application) --
the legitimate borrower flow (which does have that token) is unaffected, but a
stranger who only knows the app_id is not. Once a decision already exists for
that app_id, a rerun requires a staff session regardless of any token -- the
same _STAFF_ROLES gate get_application_financials already uses. The
underwriting console's own "Run decision" button
(frontend/app/underwriting/[appId]/page.tsx) already sends a staff session, so
this doesn't change that flow.
"""
from app import clients, db
from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)

_ACCESS_TOKEN = "test-access-token-abc123"

_APPLICATION_ROW = {
    "id": 10, "applicant_id": 5, "amount": 9000, "term_months": 24,
    "income": 40000, "name": "Jane Borrower", "ssn": "123456781",
    "access_token": _ACCESS_TOKEN,
}


def _fake_decision_client_post(monkeypatch, response=None):
    calls = []

    def _fake_post(base_url, path, payload, headers=None):
        calls.append((base_url, path, payload, headers))
        return response or {"outcome": "approve", "score": 700, "reason": None}

    monkeypatch.setattr(clients, "post", _fake_post)
    return calls


def test_first_decision_with_the_applications_own_access_token_runs(monkeypatch):
    """The legitimate borrower flow: the token minted at submission and handed
    back to them is round-tripped on their own "Get decision" call."""
    def _fake_query(sql, params=None):
        if "FROM decisions" in sql:
            return []  # no decision on record yet -- this is the first run
        return [_APPLICATION_ROW]

    monkeypatch.setattr(db, "query", _fake_query)
    _fake_decision_client_post(monkeypatch)

    resp = client.post("/applications/10/decision", json={"access_token": _ACCESS_TOKEN})

    assert resp.status_code == 200


def test_first_decision_by_a_stranger_who_only_guessed_the_app_id_is_forbidden(monkeypatch):
    """The exact review scenario: no decision exists yet, and the caller is
    anonymous with no access_token at all -- must not trigger a bureau pull."""
    def _fake_query(sql, params=None):
        if "FROM decisions" in sql:
            return []
        return [_APPLICATION_ROW]

    monkeypatch.setattr(db, "query", _fake_query)
    calls = _fake_decision_client_post(monkeypatch)

    resp = client.post("/applications/10/decision")

    assert resp.status_code == 403
    assert not calls  # never reached decision-service -- no bureau pull triggered


def test_first_decision_with_the_wrong_access_token_is_forbidden(monkeypatch):
    def _fake_query(sql, params=None):
        if "FROM decisions" in sql:
            return []
        return [_APPLICATION_ROW]

    monkeypatch.setattr(db, "query", _fake_query)
    calls = _fake_decision_client_post(monkeypatch)

    resp = client.post("/applications/10/decision", json={"access_token": "not-the-right-token"})

    assert resp.status_code == 403
    assert not calls


def test_first_decision_by_staff_needs_no_access_token(monkeypatch):
    def _fake_query(sql, params=None):
        if "FROM decisions" in sql:
            return []
        return [_APPLICATION_ROW]

    monkeypatch.setattr(db, "query", _fake_query)
    _fake_decision_client_post(monkeypatch)

    resp = client.post("/applications/10/decision", headers={"X-User-Role": "underwriter"})

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
