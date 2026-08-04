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

from app import clients, config, db, decision_state, intake
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


class _FakeRunDecisionTxCursor:
    """Stands in for the psycopg2 cursor db.transaction() yields for
    run_decision -- the applications row lock, the manual_reviews
    re-check (both under that same lock), the decisions INSERT ... ON
    CONFLICT DO UPDATE, the status UPDATE, and the accept_token mint all
    run through this one cursor, same as the real single transaction.
    manual_review lets a test simulate a staff decision committing between
    the outer pre-check and this transaction (the race this design closes)."""

    def __init__(self, calls, locked_status="submitted", manual_review=None):
        self.calls = calls
        self.locked_status = locked_status
        self.manual_review = manual_review
        self._last = None

    def execute(self, sql, params=None):
        self.calls.append((sql.strip(), params))
        stmt = sql.strip()
        if stmt.startswith("SELECT status FROM applications"):
            self._last = [{"status": self.locked_status}] if self.locked_status is not None else []
        elif stmt.startswith("SELECT outcome, reason, reviewer_name, reviewer_role, reviewed_at "
                              "FROM manual_reviews"):
            self._last = [self.manual_review] if self.manual_review else []
        else:
            self._last = []

    def fetchall(self):
        return self._last or []


def _stub_run_decision_transaction(monkeypatch, calls, locked_status="submitted", manual_review=None):
    cursor = _FakeRunDecisionTxCursor(calls, locked_status, manual_review)

    @contextlib.contextmanager
    def _fake_tx():
        yield cursor

    monkeypatch.setattr(db, "transaction", _fake_tx)
    return cursor


def test_first_decision_with_the_applications_own_access_token_runs(monkeypatch):
    """The legitimate no-account borrower flow: the access_token minted at
    submission and handed back is round-tripped on the "Get decision" call."""
    calls = []

    def _fake_query(sql, params=None):
        if "FROM decisions" in sql:
            return []  # no decision on record yet -- this is the first run
        return [_APPLICATION_ROW]

    monkeypatch.setattr(db, "query", _fake_query)
    _fake_decision_client_post(monkeypatch)
    _stub_run_decision_transaction(monkeypatch, calls)

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
    calls = []

    def _fake_query(sql, params=None):
        if "FROM decisions" in sql:
            return []
        return [_APPLICATION_ROW]

    monkeypatch.setattr(db, "query", _fake_query)
    _fake_decision_client_post(monkeypatch)
    _stub_run_decision_transaction(monkeypatch, calls)

    resp = client.post("/applications/10/decision", json={"access_token": _ACCESS_TOKEN})

    assert resp.status_code == 200
    token = resp.json()["accept_token"]
    assert token  # a real, non-empty token was minted
    update_calls = [c for c in calls if c[0].startswith("UPDATE applications SET accept_token_hash")]
    # Only the sha256 hash is ever persisted -- never the raw token itself.
    assert update_calls and update_calls[0][1][0] == decision_state.hash_accept_token(token)
    assert token not in update_calls[0][1]


def test_denied_decision_mints_no_accept_token(monkeypatch):
    calls = []

    def _fake_query(sql, params=None):
        if "FROM decisions" in sql:
            return []
        return [_APPLICATION_ROW]

    monkeypatch.setattr(db, "query", _fake_query)
    _fake_decision_client_post(monkeypatch, response={"outcome": "deny", "score": 500, "reason": "low score"})
    _stub_run_decision_transaction(monkeypatch, calls)

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
    calls = []

    def _fake_query(sql, params=None):
        if "FROM decisions" in sql:
            return [{"app_id": 10}]
        if "FROM manual_reviews" in sql:
            return []  # not manually reviewed -- a plain automated rerun is fine
        return [_APPLICATION_ROW]

    monkeypatch.setattr(db, "query", _fake_query)
    _fake_decision_client_post(monkeypatch)
    _stub_run_decision_transaction(monkeypatch, calls)

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


def test_rerun_blocked_when_a_manual_review_commits_during_the_decision_service_call(monkeypatch):
    """Architecture fix: decision-service no longer writes `decisions`
    itself (it only proposes an outcome + records its own decision_events
    audit row) -- origination-service is the sole writer, under a lock on
    the SAME applications row review_application's own transaction locks.
    Simulated here: the cheap pre-call check sees no manual review yet
    (passes), decision-service responds with a proposed outcome, but by the
    time this request's OWN transaction opens and takes the lock, a manual
    review has landed (as if review_application's transaction committed
    first) -- the authoritative in-transaction re-check (under the lock)
    must catch this, never write decisions/applications.status/
    accept_token, and report the real, effective decision."""
    calls = []

    def _fake_query(sql, params=None):
        if "FROM decisions" in sql:
            return [{"app_id": 10}]
        if "FROM manual_reviews" in sql:
            return []  # the cheap pre-call check -- nothing yet
        return [_APPLICATION_ROW]

    monkeypatch.setattr(db, "query", _fake_query)
    _fake_decision_client_post(monkeypatch)
    _stub_run_decision_transaction(
        monkeypatch, calls,
        manual_review={
            "outcome": "approve", "reason": "DTI recalculated under 43%",
            "reviewer_name": "Priya Nair", "reviewer_role": "csr",
            "reviewed_at": "2026-08-01T12:05:00+00:00",
        },
    )

    resp = client.post(
        "/applications/10/decision",
        headers={"X-User-Role": "underwriter", "X-Internal-Token": config.INTERNAL_SERVICE_TOKEN},
    )

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "manually APPROVED" in detail
    assert "Priya Nair" in detail
    # The race was caught under the lock, inside the transaction -- no
    # decisions INSERT, no status UPDATE, no accept_token mint ever ran.
    assert not any(c[0].startswith("INSERT INTO decisions") for c in calls)
    assert not any(c[0].startswith("UPDATE applications") for c in calls)


# --- POST /{app_id}/accept ---------------------------------------------------

_ACCEPT_TOKEN = "real-token-abc123"
_ACCEPT_TOKEN_HASH = decision_state.hash_accept_token(_ACCEPT_TOKEN)


def _accept_row(status="approved", outcome="approve", apr=9.99,
                 token_hash=_ACCEPT_TOKEN_HASH, token_live=True, token_consumed_at=None):
    return {
        "amount": 9000, "term_months": 24, "status": status,
        "name": "Jane Borrower", "apr": apr, "outcome": outcome,
        "accept_token_hash": token_hash, "accept_token_consumed_at": token_consumed_at,
        "token_live": token_live,
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


class _FakeAcceptTxCursor:
    """Stands in for the psycopg2 cursor db.transaction() yields for
    accept_offer -- the applications row lock (FOR UPDATE, with the hashed
    token/expiry/consumed columns), the decisions outcome re-check, the
    offer re-check, the atomic board+consume UPDATE, and the two
    board_to_servicing_tx INSERTs, all under this one cursor, same as the
    real single transaction.

    A concurrent accept that already won the race is now simulated via
    locked_status="funded" (the FOR UPDATE re-check catches it immediately,
    before the decisions/offer/board statements ever run) -- not via a
    conditional UPDATE return value like the pre-hash version of this test.
    raise_on_loan_insert lets a test simulate the loans_app_id_key backstop
    firing on the INSERT INTO loans specifically.
    """

    def __init__(self, loan_id=999, locked_status="approved", locked_outcome="approve",
                 token_hash=_ACCEPT_TOKEN_HASH, token_live=True, token_consumed_at=None,
                 offer_apr=9.99):
        self.loan_id = loan_id
        self.locked_status = locked_status
        self.locked_outcome = locked_outcome
        self.token_hash = token_hash
        self.token_live = token_live
        self.token_consumed_at = token_consumed_at
        self.offer_apr = offer_apr
        self.executed = []
        self.raise_on_loan_insert = None
        self._last = None

    def execute(self, sql, params=None):
        self.executed.append((sql.strip(), params))
        stmt = sql.strip()
        if stmt.startswith("SELECT status, accept_token_hash"):
            self._last = [{
                "status": self.locked_status,
                "accept_token_hash": self.token_hash,
                "accept_token_consumed_at": self.token_consumed_at,
                "token_live": self.token_live,
            }]
        elif stmt.startswith("SELECT outcome FROM decisions"):
            self._last = [{"outcome": self.locked_outcome}] if self.locked_outcome else []
        elif stmt.startswith("SELECT apr FROM offers"):
            self._last = [{"apr": self.offer_apr}] if self.offer_apr is not None else []
        elif stmt.startswith("UPDATE applications SET status = 'funded'"):
            self._last = None
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


def _stub_transaction(monkeypatch, loan_id=999, locked_status="approved", locked_outcome="approve",
                       token_hash=_ACCEPT_TOKEN_HASH, token_live=True, token_consumed_at=None,
                       offer_apr=9.99):
    cursor = _FakeAcceptTxCursor(
        loan_id, locked_status, locked_outcome, token_hash, token_live, token_consumed_at, offer_apr,
    )

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


def test_first_accept_rejects_expired_token(monkeypatch):
    """Security fix: expiry is enforced server-side (Postgres's own now(),
    computed as the token_live boolean) -- an expired token must be
    rejected with a clear, specific message, not the generic 403."""
    monkeypatch.setattr(db, "query", lambda sql, params=None: [_accept_row(status="approved", token_live=False)])
    board_calls = _stub_board_to_servicing(monkeypatch)

    resp = client.post("/applications/10/accept", json={"accept_token": _ACCEPT_TOKEN})

    assert resp.status_code == 409
    assert "expired" in resp.json()["detail"]
    assert not board_calls


def test_first_accept_rejects_already_consumed_token(monkeypatch):
    """Security fix: single-use -- a token that already boarded a loan once
    must not work again, independent of the hash/expiry checks."""
    monkeypatch.setattr(
        db, "query",
        lambda sql, params=None: [_accept_row(status="approved", token_consumed_at="2026-08-01T00:00:00+00:00")],
    )
    board_calls = _stub_board_to_servicing(monkeypatch)

    resp = client.post("/applications/10/accept", json={"accept_token": _ACCEPT_TOKEN})

    assert resp.status_code == 409
    assert "already been used" in resp.json()["detail"]
    assert not board_calls


def test_first_accept_succeeds_with_the_correct_accept_token(monkeypatch):
    monkeypatch.setattr(db, "query", lambda sql, params=None: [_accept_row(status="approved")])
    cursor = _stub_transaction(monkeypatch, loan_id=777)

    resp = client.post("/applications/10/accept", json={"accept_token": _ACCEPT_TOKEN})

    assert resp.status_code == 200
    assert resp.json()["loan_id"] == 777
    # The status flip clears the hash and stamps consumed_at too -- one-time use.
    board_update = next(c for c in cursor.executed if c[0].startswith("UPDATE applications SET status = 'funded'"))
    assert "accept_token_hash = NULL" in board_update[0]
    assert "accept_token_consumed_at = now()" in board_update[0]
    assert "status <> 'funded'" in board_update[0]


def test_first_accept_succeeds_for_staff_without_a_token(monkeypatch):
    monkeypatch.setattr(db, "query", lambda sql, params=None: [_accept_row(status="approved")])
    _stub_transaction(monkeypatch, loan_id=888)

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
    check and both board a loan. The FOR UPDATE re-check inside the
    transaction is the real guard -- a caller who loses the race sees
    status already 'funded' the instant it acquires the lock, and never
    reaches the decisions/offer/board statements."""
    monkeypatch.setattr(db, "query", lambda sql, params=None: [_accept_row(status="approved")])
    cursor = _stub_transaction(monkeypatch, locked_status="funded")

    resp = client.post("/applications/10/accept", json={"accept_token": _ACCEPT_TOKEN})

    assert resp.status_code == 409
    # Only the FOR UPDATE SELECT ran -- the loser never reaches the board INSERTs.
    assert len(cursor.executed) == 1


def test_first_accept_rejected_when_decision_no_longer_approved_under_lock(monkeypatch):
    """Security fix (audit finding): the pre-check read outcome == 'approve'
    before the transaction opened -- a rerun/correction could flip it in
    that gap. The authoritative re-check inside the lock must catch this
    even though the stale pre-check read passed."""
    monkeypatch.setattr(db, "query", lambda sql, params=None: [_accept_row(status="approved")])
    cursor = _stub_transaction(monkeypatch, locked_outcome="deny")

    resp = client.post("/applications/10/accept", json={"accept_token": _ACCEPT_TOKEN})

    assert resp.status_code == 422
    assert not any(c[0].startswith("UPDATE applications SET status = 'funded'") for c in cursor.executed)


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
    cursor = _stub_transaction(monkeypatch)
    cursor.raise_on_loan_insert = psycopg2.errors.UniqueViolation("dup")

    resp = client.post("/applications/10/accept", json={"accept_token": _ACCEPT_TOKEN})

    assert resp.status_code == 409
    assert resp.json()["detail"] == "a loan already exists for this application"
