"""Authz tests for POST /applications/{app_id}/decision and /accept.

Review finding: the gateway proxies every /los/* path anonymously
(gateway/app/main.py:138-143 -- an applicant has no account yet, so this is
by design for public submission/status routes), but neither /decision nor
/accept had any role/ownership check of their own -- anyone who guessed an
application id could rerun decisioning on a stranger's application, or
board/fund a real loan for one that was never even approved.

These tests cover the fix: the first decision for an application still runs
anonymously (the legitimate no-account borrower flow in
frontend/app/apply/page.tsx is unaffected), but a decision RERUN or a
re-ACCEPT of an already-funded application now requires a staff session, and
accept now refuses an application that was never actually approved.

Review finding (follow-up): a FRESH accept (not yet funded) still ran fully
anonymously with no ownership check at all -- app_id is a sequential,
guessable integer, so anyone could accept/fund a STRANGER's approved
application. It also raced: two concurrent accepts on the same
not-yet-funded application both passed the same stale-read status check and
both boarded a loan. These tests also cover that fix: a fresh accept now
requires either a staff session or the one-time accept_token minted onto the
application when it was approved (run_decision), and the actual boarding
runs through an atomic conditional UPDATE (db.transaction()) that only one
concurrent caller can win.
"""
import contextlib

import psycopg2.errors

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


def test_approved_decision_mints_an_accept_token(monkeypatch):
    """Review fix: accept_offer now requires either staff or this one-time
    token for a fresh accept -- it has to actually be minted (and returned to
    the caller) whenever the decision is an approval."""
    update_calls = []

    def _fake_query(sql, params=None):
        if "FROM decisions" in sql:
            return []
        if sql.strip().startswith("UPDATE applications SET accept_token"):
            update_calls.append(params)
            return []
        return [_APPLICATION_ROW]

    monkeypatch.setattr(db, "query", _fake_query)
    _fake_decision_client_post(monkeypatch)

    resp = client.post("/applications/10/decision")

    assert resp.status_code == 200
    token = resp.json()["accept_token"]
    assert token  # a real, non-empty token was minted
    assert update_calls and update_calls[0][0] == token  # and persisted onto the app


def test_denied_decision_mints_no_accept_token(monkeypatch):
    def _fake_query(sql, params=None):
        if "FROM decisions" in sql:
            return []
        return [_APPLICATION_ROW]

    monkeypatch.setattr(db, "query", _fake_query)
    _fake_decision_client_post(monkeypatch, response={"outcome": "deny", "score": 500, "reason": "low score"})

    resp = client.post("/applications/10/decision")

    assert resp.status_code == 200
    assert resp.json()["accept_token"] is None


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

_ACCEPT_TOKEN = "real-token-abc123"


def _accept_row(status="approved", outcome="approve", apr=9.99, accept_token=_ACCEPT_TOKEN):
    return {
        "amount": 9000, "term_months": 24, "status": status,
        "name": "Jane Borrower", "apr": apr, "outcome": outcome,
        "accept_token": accept_token,
    }


def _stub_board_to_servicing(monkeypatch, loan_id=555, raises=None):
    calls = []

    def _fake(*a, **k):
        calls.append((a, k))
        if raises:
            raise raises
        return loan_id

    monkeypatch.setattr(intake, "board_to_servicing", _fake)
    return calls


class _FakeTxCursor:
    """Stands in for the psycopg2 cursor db.transaction() yields --
    simulates the atomic conditional UPDATE (claim_succeeds toggles whether
    a concurrent accept already won the race) and the two board_to_
    servicing_tx INSERTs."""

    def __init__(self, claim_succeeds, loan_id):
        self.claim_succeeds = claim_succeeds
        self.loan_id = loan_id
        self.executed = []
        self._last = None

    def execute(self, sql, params=None):
        self.executed.append((sql.strip(), params))
        stmt = sql.strip()
        if stmt.startswith("UPDATE applications"):
            self._last = [{"id": params[0]}] if self.claim_succeeds else []
        elif stmt.startswith("INSERT INTO loans"):
            self._last = {"id": self.loan_id}
        elif stmt.startswith("INSERT INTO balances"):
            self._last = None
        else:
            raise AssertionError(f"unexpected tx statement: {sql}")

    def fetchall(self):
        return self._last or []

    def fetchone(self):
        return self._last


def _stub_transaction(monkeypatch, claim_succeeds=True, loan_id=999):
    cursor = _FakeTxCursor(claim_succeeds, loan_id)

    @contextlib.contextmanager
    def _fake_tx():
        yield cursor

    monkeypatch.setattr(db, "transaction", _fake_tx)
    return cursor


def test_first_accept_rejects_anonymous_caller_with_no_token(monkeypatch):
    """The review's exact finding: app_id is sequential/guessable, so a fresh
    accept must not succeed for just anyone who can guess it."""
    monkeypatch.setattr(db, "query", lambda sql, params=None: [_accept_row(status="approved")])
    board_calls = _stub_board_to_servicing(monkeypatch)

    resp = client.post("/applications/10/accept")

    assert resp.status_code == 403
    assert not board_calls


def test_first_accept_rejects_wrong_token(monkeypatch):
    monkeypatch.setattr(db, "query", lambda sql, params=None: [_accept_row(status="approved")])
    board_calls = _stub_board_to_servicing(monkeypatch)

    resp = client.post("/applications/10/accept", json={"accept_token": "attacker-guessed-token"})

    assert resp.status_code == 403
    assert not board_calls


def test_first_accept_succeeds_with_the_correct_accept_token(monkeypatch):
    monkeypatch.setattr(db, "query", lambda sql, params=None: [_accept_row(status="approved")])
    cursor = _stub_transaction(monkeypatch, claim_succeeds=True, loan_id=777)

    resp = client.post("/applications/10/accept", json={"accept_token": _ACCEPT_TOKEN})

    assert resp.status_code == 200
    assert resp.json()["loan_id"] == 777
    # The status flip clears accept_token too -- one-time use.
    update_sql = cursor.executed[0][0]
    assert "accept_token = NULL" in update_sql
    assert "status <> 'funded'" in update_sql


def test_first_accept_succeeds_for_staff_without_a_token(monkeypatch):
    monkeypatch.setattr(db, "query", lambda sql, params=None: [_accept_row(status="approved")])
    _stub_transaction(monkeypatch, claim_succeeds=True, loan_id=888)

    resp = client.post("/applications/10/accept", headers={"X-User-Role": "csr"})

    assert resp.status_code == 200
    assert resp.json()["loan_id"] == 888


def test_first_accept_returns_409_when_a_concurrent_accept_already_won(monkeypatch):
    """The review's race-condition finding: two concurrent accepts on the same
    not-yet-funded application both used to pass the same stale-read status
    check and both board a loan. The atomic UPDATE is the real guard -- a
    caller who loses the race gets 0 rows back and never boards anything."""
    monkeypatch.setattr(db, "query", lambda sql, params=None: [_accept_row(status="approved")])
    cursor = _stub_transaction(monkeypatch, claim_succeeds=False)

    resp = client.post("/applications/10/accept", json={"accept_token": _ACCEPT_TOKEN})

    assert resp.status_code == 409
    # Only the UPDATE ran -- the loser never reaches the board INSERTs.
    assert len(cursor.executed) == 1


def test_accept_rejects_application_that_was_never_approved(monkeypatch):
    """The other half of the review finding: accept never checked the
    decision outcome at all -- a denied or still-pending application could be
    boarded/funded like any other."""
    monkeypatch.setattr(db, "query", lambda sql, params=None: [_accept_row(status="submitted", outcome="deny")])
    board_calls = _stub_board_to_servicing(monkeypatch)

    resp = client.post("/applications/10/accept", json={"accept_token": _ACCEPT_TOKEN})

    assert resp.status_code == 422
    assert not board_calls  # never boards/funds a loan for a non-approved application


def test_reaccept_of_an_already_funded_application_requires_staff(monkeypatch):
    monkeypatch.setattr(db, "query", lambda sql, params=None: [_accept_row(status="funded")])
    board_calls = _stub_board_to_servicing(monkeypatch)

    resp = client.post("/applications/10/accept", json={"accept_token": _ACCEPT_TOKEN})

    assert resp.status_code == 403
    assert not board_calls  # never re-boards/re-funds


def test_reaccept_of_an_already_funded_application_succeeds_for_staff(monkeypatch):
    monkeypatch.setattr(db, "query", lambda sql, params=None: [_accept_row(status="funded")])
    _stub_board_to_servicing(monkeypatch)

    resp = client.post("/applications/10/accept", headers={"X-User-Role": "underwriter"})

    assert resp.status_code == 200


def test_reaccept_reports_409_when_a_loan_already_exists(monkeypatch):
    """loans_app_id_key (db/migrations/0013) is the database-level backstop --
    if a staff re-accept somehow races a loan that already exists for this
    app_id, surface a clean 409 instead of a raw 500."""
    monkeypatch.setattr(db, "query", lambda sql, params=None: [_accept_row(status="funded")])
    _stub_board_to_servicing(monkeypatch, raises=psycopg2.errors.UniqueViolation("dup"))

    resp = client.post("/applications/10/accept", headers={"X-User-Role": "underwriter"})

    assert resp.status_code == 409
