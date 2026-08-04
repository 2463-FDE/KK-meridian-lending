"""Authz tests for POST /applications/{app_id}/decision and /accept.

Review finding: the gateway proxies every /los/* path anonymously
(gateway/app/main.py:138-143 -- an applicant has no account yet, so this is
by design for public submission/status routes), but neither /decision nor
/accept had any role/ownership check of their own -- anyone who guessed an
application id could rerun decisioning on a stranger's application, or
board/fund a real loan for one that was never even approved.

These tests cover the fix: the first decision for an application still runs
for the legitimate no-account borrower flow (frontend/app/apply/page.tsx),
now proven via the access_token minted onto the application at submission
(merged in from main's own review fix -- see intake.create_application /
ApplicationCreated.access_token), not just anonymously -- anyone who merely
guessed an app_id with no token is rejected. A decision RERUN or a
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

Bug found in the field (test_manual_review.py's own feature): a rerun was
only ever staff-gated, nothing else. Since scoring is deterministic (same
SSN/income -> same score), rerunning a decision on an already-funded
application silently reset its recorded outcome back to the automated
result while the loan sat funded on top of it, and rerunning after a manual
review (routers/applications.py's review_application) reset a staff
decision back to "refer" -- making the SAME application eligible for manual
review again, and again, indefinitely. These tests cover the fix: a rerun
now 422s on either an already-funded application or one with a manual
review on record.
"""
import contextlib

import psycopg2.errors

from app import clients, config, db, intake
from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)

_ACCESS_TOKEN = "real-access-token-xyz789"

_APPLICATION_ROW = {
    "id": 10, "applicant_id": 5, "amount": 9000, "term_months": 24,
    "income": 40000, "name": "Jane Borrower", "ssn": "123456781",
    "access_token": _ACCESS_TOKEN,
}


# --- POST /{app_id}/decision -------------------------------------------------

def _fake_decision_client_post(monkeypatch, response=None):
    calls = []

    def _fake_post(base_url, path, payload, headers=None):
        calls.append((base_url, path, payload, headers))
        return response or {"outcome": "approve", "score": 700, "reason": None}

    monkeypatch.setattr(clients, "post", _fake_post)
    return calls


def test_first_decision_with_the_applications_own_access_token_runs(monkeypatch):
    """The legitimate no-account borrower flow: the access_token minted at
    submission and handed back is round-tripped on the "Get decision" call."""
    def _fake_query(sql, params=None):
        if "FROM decisions" in sql:
            return []  # no decision on record yet -- this is the first run
        return [_APPLICATION_ROW]

    monkeypatch.setattr(db, "query", _fake_query)
    _fake_decision_client_post(monkeypatch)

    resp = client.post("/applications/10/decision", json={"access_token": _ACCESS_TOKEN})

    assert resp.status_code == 200


def test_first_decision_by_a_stranger_who_only_guessed_the_app_id_is_forbidden(monkeypatch):
    """Merged in from main's own review fix: no decision exists yet, and the
    caller is anonymous with no access_token -- must not trigger a bureau pull."""
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

    resp = client.post("/applications/10/decision", json={"access_token": "attacker-guessed-token"})

    assert resp.status_code == 403
    assert not calls


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

    resp = client.post("/applications/10/decision", json={"access_token": _ACCESS_TOKEN})

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

    resp = client.post("/applications/10/decision", json={"access_token": _ACCESS_TOKEN})

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
        if "FROM manual_reviews" in sql:
            return []  # not manually reviewed -- a plain automated rerun is fine
        return [_APPLICATION_ROW]

    monkeypatch.setattr(db, "query", _fake_query)
    _fake_decision_client_post(monkeypatch)

    resp = client.post(
        "/applications/10/decision",
        headers={"X-User-Role": "underwriter", "X-Internal-Token": config.INTERNAL_SERVICE_TOKEN},
    )

    assert resp.status_code == 200


def test_rerun_of_an_existing_decision_by_staff_without_internal_token_is_forbidden(monkeypatch):
    """Review fix: X-User-Role alone must not be enough -- a caller who skips
    the gateway (e.g. origination-service's host port were ever reopened)
    could set X-User-Role: admin itself with nothing to verify the claim."""
    def _fake_query(sql, params=None):
        if "FROM decisions" in sql:
            return [{"app_id": 10}]
        return [_APPLICATION_ROW]

    monkeypatch.setattr(db, "query", _fake_query)
    calls = _fake_decision_client_post(monkeypatch)

    resp = client.post("/applications/10/decision", headers={"X-User-Role": "underwriter"})

    assert resp.status_code == 403
    assert not calls


def test_rerun_of_an_already_funded_application_is_rejected(monkeypatch):
    """Bug found in the field: scoring is deterministic, so a rerun on a
    funded application used to silently reset its recorded decision back to
    the automated outcome while the loan sat funded on top of it."""
    funded_row = {**_APPLICATION_ROW, "status": "funded"}

    def _fake_query(sql, params=None):
        if "FROM decisions" in sql:
            return [{"app_id": 10}]
        return [funded_row]

    monkeypatch.setattr(db, "query", _fake_query)
    calls = _fake_decision_client_post(monkeypatch)

    resp = client.post(
        "/applications/10/decision",
        headers={"X-User-Role": "underwriter", "X-Internal-Token": config.INTERNAL_SERVICE_TOKEN},
    )

    assert resp.status_code == 422
    assert not calls  # never reached decision-service -- no bureau pull triggered


def test_rerun_of_a_manually_reviewed_application_is_rejected(monkeypatch):
    """Bug found in the field: a rerun after a manual review (routers/
    applications.py's review_application) silently reset the outcome back to
    "refer", making the same application eligible for manual review again --
    and again, indefinitely (observed: 5 flip-flopped reviews on one app).

    Review fix: the block message used to be generic ("resolved by staff");
    it now states the actual outcome/staff member/timestamp/reason, and the
    status code is 409 (a real conflict), not 422."""
    def _fake_query(sql, params=None):
        if "FROM decisions" in sql:
            return [{"app_id": 10}]
        if "FROM manual_reviews" in sql:
            return [{
                "outcome": "deny", "reason": "DTI too high after manual re-verification",
                "reviewer_name": "Sam Okafor", "reviewer_role": "underwriter",
                "reviewed_at": "2026-08-01T12:00:00+00:00",
            }]
        return [_APPLICATION_ROW]

    monkeypatch.setattr(db, "query", _fake_query)
    calls = _fake_decision_client_post(monkeypatch)

    resp = client.post(
        "/applications/10/decision",
        headers={"X-User-Role": "underwriter", "X-Internal-Token": config.INTERNAL_SERVICE_TOKEN},
    )

    assert resp.status_code == 409
    assert not calls
    detail = resp.json()["detail"]
    assert "manually DENIED" in detail
    assert "Sam Okafor" in detail
    assert "DTI too high after manual re-verification" in detail
    assert "cannot be rerun" in detail


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
    a concurrent accept already won the race), the OFFER_ACCEPTED stamp
    (db/migrations/0021), and the two board_to_servicing_tx INSERTs.
    raise_on_loan_insert lets a test simulate the loans_app_id_key backstop
    firing on the INSERT INTO loans specifically."""

    def __init__(self, claim_succeeds, loan_id):
        self.claim_succeeds = claim_succeeds
        self.loan_id = loan_id
        self.executed = []
        self.raise_on_loan_insert = None
        self._last = None

    def execute(self, sql, params=None):
        self.executed.append((sql.strip(), params))
        stmt = sql.strip()
        if stmt.startswith("UPDATE applications"):
            self._last = [{"id": params[0]}] if self.claim_succeeds else []
        elif stmt.startswith("UPDATE offers SET accepted_at"):
            self._last = None
        elif stmt.startswith("INSERT INTO loans"):
            if self.raise_on_loan_insert:
                raise self.raise_on_loan_insert
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

    resp = client.post(
        "/applications/10/accept",
        headers={"X-User-Role": "csr", "X-Internal-Token": config.INTERNAL_SERVICE_TOKEN},
    )

    assert resp.status_code == 200
    assert resp.json()["loan_id"] == 888


def test_first_accept_by_staff_without_internal_token_is_forbidden(monkeypatch):
    monkeypatch.setattr(db, "query", lambda sql, params=None: [_accept_row(status="approved")])
    board_calls = _stub_board_to_servicing(monkeypatch)

    resp = client.post("/applications/10/accept", headers={"X-User-Role": "csr"})

    assert resp.status_code == 403
    assert not board_calls


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


# test_accept_rejects_application_that_was_never_approved (the review's
# original finding: accept never checked the decision outcome at all) is now
# covered more precisely by test_accept_rejects_a_denied_application_with_
# its_reason and test_accept_rejects_a_still_pending_application below.


def test_reaccept_of_an_already_funded_application_is_rejected_with_no_token(monkeypatch):
    """Requirement: 'This application has already been boarded.' -- a hard
    block, not a staff-only re-board path (that path is removed: the atomic
    funding transaction already prevents the partial state -- funded status
    with no loan -- it used to exist to recover from)."""
    monkeypatch.setattr(db, "query", lambda sql, params=None: [_accept_row(status="funded")])
    board_calls = _stub_board_to_servicing(monkeypatch)

    resp = client.post("/applications/10/accept", json={"accept_token": _ACCEPT_TOKEN})

    assert resp.status_code == 409
    assert resp.json()["detail"] == "This application has already been boarded."
    assert not board_calls


def test_reaccept_of_an_already_funded_application_is_rejected_for_staff_too(monkeypatch):
    """Requirement: already-boarded blocks EVERYONE, staff included -- there
    is no more re-board escape hatch."""
    monkeypatch.setattr(db, "query", lambda sql, params=None: [_accept_row(status="funded")])
    board_calls = _stub_board_to_servicing(monkeypatch)

    resp = client.post(
        "/applications/10/accept",
        headers={"X-User-Role": "underwriter", "X-Internal-Token": config.INTERNAL_SERVICE_TOKEN},
    )

    assert resp.status_code == 409
    assert resp.json()["detail"] == "This application has already been boarded."
    assert not board_calls


def test_reaccept_of_an_already_funded_application_by_staff_without_internal_token_is_still_rejected(monkeypatch):
    """The already-boarded check fires before the auth check now (it's the
    same status/decision/offer info GET already exposes anonymously) -- a
    staff caller with no internal token gets the same 409, not a 403."""
    monkeypatch.setattr(db, "query", lambda sql, params=None: [_accept_row(status="funded")])
    board_calls = _stub_board_to_servicing(monkeypatch)

    resp = client.post("/applications/10/accept", headers={"X-User-Role": "underwriter"})

    assert resp.status_code == 409
    assert resp.json()["detail"] == "This application has already been boarded."
    assert not board_calls


def test_first_accept_returns_409_when_no_offer_exists_yet(monkeypatch):
    """Review fix: run_decision's auto_generate_offer call is best-effort, so
    an approved application can reach accept_offer with no linked offer row
    at all. This used to fall back to a hardcoded 7.99 APR and board the
    borrower at a rate/terms nobody ever showed them -- no TILA disclosure on
    record. Must fail closed (409) with a specific, actionable message."""
    monkeypatch.setattr(db, "query", lambda sql, params=None: [_accept_row(status="approved", apr=None)])
    board_calls = _stub_board_to_servicing(monkeypatch)

    resp = client.post("/applications/10/accept", json={"accept_token": _ACCEPT_TOKEN})

    assert resp.status_code == 409
    assert resp.json()["detail"] == "Create an offer before boarding this application."
    assert not board_calls


def test_accept_rejects_a_denied_application_with_its_reason(monkeypatch):
    """Requirement: 'This application cannot be boarded because it was
    denied. Reason: [decision reason].'"""
    def _fake_query(sql, params=None):
        if "FROM decision_events" in sql:
            return [{"reason_codes": ["Low credit bureau score relative to lending criteria"]}]
        if "FROM manual_reviews" in sql:
            return []  # automated-only deny -- no staff review on record
        return [_accept_row(status="denied", outcome="deny")]

    monkeypatch.setattr(db, "query", _fake_query)
    board_calls = _stub_board_to_servicing(monkeypatch)

    resp = client.post("/applications/10/accept", json={"accept_token": _ACCEPT_TOKEN})

    assert resp.status_code == 422
    assert resp.json()["detail"] == (
        "This application cannot be boarded because it was denied. "
        "Reason: Low credit bureau score relative to lending criteria."
    )
    assert not board_calls


def test_accept_rejects_a_still_pending_application(monkeypatch):
    """Requirement: 'This application must receive final approval before it
    can be boarded.' -- covers 'refer' and no-decision-yet alike."""
    monkeypatch.setattr(db, "query", lambda sql, params=None: [_accept_row(status="in_review", outcome="refer")])
    board_calls = _stub_board_to_servicing(monkeypatch)

    resp = client.post("/applications/10/accept", json={"accept_token": _ACCEPT_TOKEN})

    assert resp.status_code == 422
    assert resp.json()["detail"] == "This application must receive final approval before it can be boarded."
    assert not board_calls


def test_accept_reports_409_when_a_loan_already_exists(monkeypatch):
    """loans_app_id_key (db/migrations/0015) is the database-level backstop --
    if the loans INSERT somehow races a loan that already exists for this
    app_id (e.g. the legacy direct-board endpoint), surface a clean 409
    instead of a raw 500. This is now exercised on the NORMAL (not-yet-
    funded) accept path -- the already-funded case is hard-blocked before
    ever reaching a board attempt at all (see the already-boarded tests)."""
    monkeypatch.setattr(db, "query", lambda sql, params=None: [_accept_row(status="approved")])
    cursor = _stub_transaction(monkeypatch, claim_succeeds=True)
    cursor.raise_on_loan_insert = psycopg2.errors.UniqueViolation("dup")

    resp = client.post("/applications/10/accept", json={"accept_token": _ACCEPT_TOKEN})

    assert resp.status_code == 409
    assert resp.json()["detail"] == "a loan already exists for this application"
