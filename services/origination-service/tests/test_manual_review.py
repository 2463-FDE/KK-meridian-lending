"""Tests for POST /applications/{app_id}/review (db/migrations/0018, 0020).

Feature: a staff tool to resolve a "refer" decision (policies/underwriting_
guidelines.md's manual-review band, score 600-659 or DTI 43-50%), or to
record staff's own approve/deny outright. Requirement: once staff decides,
that decision is FINAL -- no staff member (not even a different one) may
change it afterward, a reason is required up front, and two simultaneous
decisions can't both win. These tests cover: the first decision succeeds, a
reason is required, a second decision attempt is rejected with the exact
"already decided" message and writes nothing, the original decision/reason
survive untouched, a concurrent second attempt loses the race atomically
(not just via an application-level check), and unauthorized callers are
rejected.
"""
import contextlib

from app import config, db
from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)

_STAFF_HEADERS = {"X-User-Role": "underwriter", "X-Internal-Token": config.INTERNAL_SERVICE_TOKEN}

_PRIOR_APPROVE = {
    "outcome": "approve",
    "reason": "DTI recalculated under 43% with updated income",
    "reviewer_name": "Sam Okafor",
    "reviewer_role": "underwriter",
    "reviewed_at": "2026-08-01T12:00:00+00:00",
}


def _fake_query(decision_outcome, calls=None, app_status="in_review", prior_review=None, user_row=None):
    """Mirrors test_decision_and_accept_authz.py's substring-routed fake db."""

    def _query(sql, params=None):
        if calls is not None:
            calls.append((sql, params))
        stmt = sql.strip()
        if stmt.startswith("SELECT id, status FROM applications"):
            return [{"id": 10, "status": app_status}]
        if stmt.startswith("SELECT outcome, reason, reviewer_name, reviewer_role, reviewed_at "
                            "FROM manual_reviews"):
            return [prior_review] if prior_review else []
        if stmt.startswith("SELECT display_name, username FROM users"):
            return [user_row] if user_row else []
        if "FROM decisions" in stmt:
            return [{"outcome": decision_outcome}] if decision_outcome else []
        return []

    return _query


class _FakeReviewTxCursor:
    """Stands in for the psycopg2 cursor db.transaction() yields for
    review_application -- the row-locking status re-check, the atomic
    INSERT ... ON CONFLICT DO NOTHING onto manual_reviews, the decisions
    UPDATE, the status UPDATE, and the accept_token mint/clear all run
    through this one cursor, same as the real single transaction."""

    def __init__(self, claim_succeeds, calls, locked_status="in_review", winning_row=None, locked_outcome="refer"):
        self.claim_succeeds = claim_succeeds
        self.calls = calls
        self.locked_status = locked_status
        self.winning_row = winning_row or _PRIOR_APPROVE
        self.locked_outcome = locked_outcome
        self._last = None

    def execute(self, sql, params=None):
        self.calls.append((sql.strip(), params))
        stmt = sql.strip()
        if stmt.startswith("SELECT status FROM applications"):
            self._last = [{"status": self.locked_status}]
        elif stmt.startswith("SELECT outcome FROM decisions"):
            # Audit fix: review_application re-verifies current_outcome == 'refer'
            # under a row lock inside the transaction, not just the outer
            # pre-check -- defaults to 'refer' (the happy-path case) here.
            self._last = [{"outcome": self.locked_outcome}] if self.locked_outcome is not None else []
        elif stmt.startswith("INSERT INTO manual_reviews"):
            # params = (app_id, reviewer_role, reviewer_name, outcome, reason)
            self._last = (
                [{"outcome": params[3], "reason": params[4], "reviewer_name": params[2],
                  "reviewer_role": params[1], "reviewed_at": "2026-08-04T00:00:00+00:00"}]
                if self.claim_succeeds else []
            )
        elif stmt.startswith("SELECT outcome, reason, reviewer_name, reviewer_role, reviewed_at "
                              "FROM manual_reviews"):
            # The post-loss read-back of whichever request actually won.
            self._last = [self.winning_row]
        else:
            self._last = []

    def fetchall(self):
        return self._last or []


def _stub_transaction(monkeypatch, calls, claim_succeeds=True, locked_status="in_review", winning_row=None, locked_outcome="refer"):
    cursor = _FakeReviewTxCursor(claim_succeeds, calls, locked_status, winning_row, locked_outcome)

    @contextlib.contextmanager
    def _fake_tx():
        yield cursor

    monkeypatch.setattr(db, "transaction", _fake_tx)
    return cursor


# --- authz ---------------------------------------------------------------

def test_review_requires_a_staff_session(monkeypatch):
    """No X-Internal-Token (or an anonymous/borrower call) must never be able
    to decide -- this isn't a decision the applicant can make for
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
    monkeypatch.setattr(db, "query", lambda sql, params=None: [])

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


def test_review_rejects_overriding_a_clean_automated_approve(monkeypatch):
    """This endpoint resolves a 'refer' -- it must not become a backdoor to
    override an automated approve/deny that was never eligible for manual
    review in the first place."""
    monkeypatch.setattr(db, "query", _fake_query("approve"))

    resp = client.post(
        "/applications/10/review",
        json={"outcome": "deny", "reason": "changed my mind"},
        headers=_STAFF_HEADERS,
    )

    assert resp.status_code == 422
    assert "only a 'refer' decision can be reviewed" in resp.json()["detail"]


def test_review_rejects_overriding_a_clean_automated_deny(monkeypatch):
    monkeypatch.setattr(db, "query", _fake_query("deny"))

    resp = client.post(
        "/applications/10/review",
        json={"outcome": "approve", "reason": "changed my mind"},
        headers=_STAFF_HEADERS,
    )

    assert resp.status_code == 422
    assert "only a 'refer' decision can be reviewed" in resp.json()["detail"]


# --- requirement 2: a reason is required ----------------------------------

def test_review_rejects_an_empty_reason(monkeypatch):
    monkeypatch.setattr(db, "query", _fake_query("refer"))

    resp = client.post(
        "/applications/10/review",
        json={"outcome": "deny", "reason": ""},
        headers=_STAFF_HEADERS,
    )

    assert resp.status_code == 422


def test_review_rejects_a_missing_reason(monkeypatch):
    monkeypatch.setattr(db, "query", _fake_query("refer"))

    resp = client.post(
        "/applications/10/review",
        json={"outcome": "deny"},
        headers=_STAFF_HEADERS,
    )

    assert resp.status_code == 422


# --- requirement 1: the first decision succeeds ---------------------------

def test_review_approve_records_outcome_and_mints_accept_token(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "query", _fake_query("refer", calls, user_row={"display_name": "Sam Okafor", "username": "underwriter"}))
    _stub_transaction(monkeypatch, calls)

    resp = client.post(
        "/applications/10/review",
        json={"outcome": "approve", "reason": "DTI recalculated under 43% with updated income"},
        headers={**_STAFF_HEADERS, "X-User-Id": "2"},
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
    assert audit_inserts[0][1] == (
        10, "underwriter", "Sam Okafor", "approve", "DTI recalculated under 43% with updated income",
    )

    status_updates = [c for c in calls if "SET status" in c[0]]
    assert status_updates and status_updates[0][1] == ("approved", 10)
    # Review fix parity with run_decision: never regress an already-funded row.
    assert "status <> 'funded'" in status_updates[0][0]


def test_review_deny_returns_the_staff_reason_as_adverse_action(monkeypatch):
    calls = []
    monkeypatch.setattr(db, "query", _fake_query("refer", calls))
    _stub_transaction(monkeypatch, calls)

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

    accept_token_updates = [c for c in calls if "accept_token" in c[0]]
    assert len(accept_token_updates) == 1
    assert "accept_token = NULL" in accept_token_updates[0][0]


def test_review_falls_back_to_role_when_no_user_id_or_lookup_hit(monkeypatch):
    """No X-User-Id header, or the users lookup misses -- still records the
    decision (reviewer_role is NOT NULL and always present), just with no
    resolved display name."""
    calls = []
    monkeypatch.setattr(db, "query", _fake_query("refer", calls))  # no user_row
    _stub_transaction(monkeypatch, calls)

    resp = client.post(
        "/applications/10/review",
        json={"outcome": "approve", "reason": "manual ok"},
        headers=_STAFF_HEADERS,  # no X-User-Id
    )

    assert resp.status_code == 200
    audit_inserts = [c for c in calls if c[0].strip().startswith("INSERT INTO manual_reviews")]
    assert audit_inserts[0][1] == (10, "underwriter", None, "approve", "manual ok")


# --- requirement 3 & 4: a second decision is rejected, original untouched --

def test_review_rejects_a_second_decision_with_the_exact_required_message(monkeypatch):
    """Requirement: once decided, no staff member (not even a different one)
    may change it. The exact message must name the outcome, the staff
    member, the timestamp, and the original reason, and state it cannot be
    overwritten."""
    monkeypatch.setattr(db, "query", _fake_query("approve", prior_review=_PRIOR_APPROVE))

    resp = client.post(
        "/applications/10/review",
        json={"outcome": "deny", "reason": "changed my mind"},
        headers=_STAFF_HEADERS,
    )

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "already been APPROVED" in detail
    assert "Sam Okafor" in detail
    assert "2026-08-01T12:00:00+00:00" in detail
    assert "DTI recalculated under 43% with updated income" in detail
    assert "cannot be overwritten" in detail


def test_review_second_decision_attempt_writes_nothing(monkeypatch):
    """Requirement: if someone tries to change the decision, do not update
    the database at all -- not the outcome, not the audit trail, not the
    application status."""
    calls = []
    monkeypatch.setattr(db, "query", _fake_query("approve", calls, prior_review=_PRIOR_APPROVE))

    resp = client.post(
        "/applications/10/review",
        json={"outcome": "deny", "reason": "changed my mind"},
        headers=_STAFF_HEADERS,
    )

    assert resp.status_code == 409
    # The pre-check short-circuits before a transaction is even opened --
    # no write of any kind was ever attempted.
    assert not any(c[0].strip().startswith(("UPDATE", "INSERT")) for c in calls)


def test_review_denied_application_reports_its_original_deny_unchanged(monkeypatch):
    """The original decision and reason are what get reported back, no
    matter what a later attempt tries to change them to."""
    prior_deny = {
        "outcome": "deny", "reason": "Income insufficient for requested amount",
        "reviewer_name": "Priya Nair", "reviewer_role": "csr",
        "reviewed_at": "2026-07-15T09:30:00+00:00",
    }
    monkeypatch.setattr(db, "query", _fake_query("deny", prior_review=prior_deny))

    resp = client.post(
        "/applications/10/review",
        json={"outcome": "approve", "reason": "actually let's approve it"},
        headers=_STAFF_HEADERS,
    )

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "already been DENIED" in detail
    assert "Priya Nair" in detail
    assert "Income insufficient for requested amount" in detail
    # The second attempt's own outcome/reason never appear -- only the original.
    assert "actually let's approve it" not in detail


# --- audit fix: current_outcome == 'refer' re-verified under a row lock ---

def test_review_rejects_when_outcome_changed_away_from_refer_between_precheck_and_transaction(monkeypatch):
    """Audit fix: the outer current_outcome check reads via a separate,
    autocommitted db.query() call BEFORE the transaction opens -- a
    concurrent run_decision rerun could change decisions.outcome in that
    gap. Simulated here: the outer pre-check sees 'refer' (passes), but the
    SELECT ... FOR UPDATE inside the transaction sees 'approve' (changed in
    between) -- must reject and write nothing."""
    calls = []
    monkeypatch.setattr(db, "query", _fake_query("refer", calls))  # outer pre-check sees 'refer'
    _stub_transaction(monkeypatch, calls, locked_outcome="approve")  # but it already changed

    resp = client.post(
        "/applications/10/review",
        json={"outcome": "deny", "reason": "trying to resolve a refer that no longer exists"},
        headers=_STAFF_HEADERS,
    )

    assert resp.status_code == 422
    assert "only a 'refer' decision can be reviewed" in resp.json()["detail"]
    assert not any(c[0].strip().startswith("INSERT INTO manual_reviews") for c in calls)


# --- requirement 5: two simultaneous decisions can't overwrite each other --

def test_review_concurrent_decision_loses_the_atomic_insert_race(monkeypatch):
    """Requirement: two simultaneous decisions cannot overwrite each other.
    Both requests can pass the pre-check (neither sees a prior row yet) --
    the atomic `INSERT ... ON CONFLICT (app_id) DO NOTHING` inside the
    transaction is the real guard: the loser gets zero rows back, is told
    the winner's actual decision (read back in the same transaction), and
    writes nothing else."""
    calls = []
    monkeypatch.setattr(db, "query", _fake_query("refer", calls))  # no prior row yet
    _stub_transaction(monkeypatch, calls, claim_succeeds=False, winning_row=_PRIOR_APPROVE)

    resp = client.post(
        "/applications/10/review",
        json={"outcome": "deny", "reason": "the losing request's own reason"},
        headers=_STAFF_HEADERS,
    )

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "already been APPROVED" in detail
    assert "Sam Okafor" in detail
    # The loser's own outcome/reason never committed anywhere.
    assert not any(c[0].strip().startswith("UPDATE decisions") for c in calls)
    assert not any("SET status" in c[0] for c in calls)
    assert not any("accept_token" in c[0] for c in calls)


def test_review_blocks_when_accept_offer_funds_it_between_the_precheck_and_the_transaction(monkeypatch):
    """The funded pre-check reads via db.query() -- a separate, autocommitted
    connection -- BEFORE the transaction opens. accept_offer boarding this
    same application in the gap between that read and the transaction used
    to slip right past it entirely: nothing inside the transaction
    re-checked funded status, so decisions.outcome would still flip on an
    application that's now funded. `SELECT ... FOR UPDATE` inside the
    transaction is the real, race-proof guard -- simulated here by having
    the pre-check see 'in_review' (not funded yet) while the transaction's
    own locked read sees 'funded' (funded in between)."""
    calls = []
    monkeypatch.setattr(db, "query", _fake_query("refer", calls, app_status="in_review"))
    _stub_transaction(monkeypatch, calls, locked_status="funded")

    resp = client.post(
        "/applications/10/review",
        json={"outcome": "deny", "reason": "changed my mind"},
        headers=_STAFF_HEADERS,
    )

    assert resp.status_code == 422
    assert not any(c[0].strip().startswith("UPDATE decisions") for c in calls)
    assert not any(c[0].strip().startswith("INSERT INTO manual_reviews") for c in calls)
    assert not any("SET status" in c[0] for c in calls)
