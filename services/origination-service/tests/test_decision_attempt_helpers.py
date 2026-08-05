"""PR #6 review (Finding 2) -- safe failure recording and attempt-identity
correlation.

Covers two required tests not exercised elsewhere:
  - sanitize_failure_detail/mark_attempt_failed never persist a raw
    exception, response body, or arbitrary caller-supplied text -- only
    one of the fixed, bounded templates, keyed by an allowlisted code.
  - run_decision rejects a decision-service response whose attempt_id
    doesn't match the attempt it's currently waiting on.
"""
import contextlib

from app import clients, config, db, decision_state
from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)

_ACCESS_TOKEN = "real-access-token-xyz789"
_APPLICATION_ROW = {
    "id": 10, "applicant_id": 5, "amount": 9000, "term_months": 24,
    "income": 40000, "name": "Jane Borrower", "ssn": "123456781",
    "access_token": _ACCESS_TOKEN,
}


def test_sanitize_failure_detail_only_ever_returns_a_fixed_template():
    for code in decision_state.FAILURE_CODES:
        detail = decision_state.sanitize_failure_detail(code)
        assert detail == decision_state._FAILURE_DETAIL[code]
        assert len(detail) <= decision_state.MAX_FAILURE_DETAIL_LEN


def test_sanitize_failure_detail_rejects_unknown_codes_and_never_echoes_input():
    """An unrecognized code (a typo, or -- if it ever happened -- an
    attacker-influenced value) must fall back to a fixed 'internal_error'
    template, never echo the code or any other input back verbatim."""
    attacker_supplied = "'; DROP TABLE decision_attempts; --<script>evil</script>"
    detail = decision_state.sanitize_failure_detail(attacker_supplied)

    assert detail == decision_state._FAILURE_DETAIL["internal_error"]
    assert attacker_supplied not in detail
    assert "DROP TABLE" not in detail
    assert "<script>" not in detail


def test_mark_attempt_failed_only_ever_writes_allowlisted_code_and_template(monkeypatch):
    """The actual UPDATE statement issued must carry only an allowlisted
    failure_code and its fixed template -- never a raw exception message,
    stack trace, or HTTP response body, even if a caller tried to pass one
    in as the 'code'."""
    captured = {}

    class _FakeCur:
        def execute(self, sql, params=None):
            captured["sql"] = sql.strip()
            captured["params"] = params

    @contextlib.contextmanager
    def _fake_tx():
        yield _FakeCur()

    monkeypatch.setattr(db, "transaction", _fake_tx)

    raw_exception_text = "OperationalError: could not connect to host 10.0.0.5 password=hunter2"
    decision_state.mark_attempt_failed(attempt_id=42, failure_code=raw_exception_text)

    assert captured["sql"].startswith("UPDATE decision_attempts SET state = 'failed'")
    failure_code, failure_detail, attempt_id = captured["params"][0], captured["params"][1], captured["params"][2]
    assert failure_code == "internal_error"  # never the raw exception text
    assert failure_code in decision_state.FAILURE_CODES
    assert failure_detail == decision_state._FAILURE_DETAIL["internal_error"]
    assert "hunter2" not in failure_detail
    assert "10.0.0.5" not in failure_detail
    assert attempt_id == 42


class _FakeRunDecisionTxCursor:
    def __init__(self, locked_status="submitted", manual_review=None, attempt_id=1):
        self.locked_status = locked_status
        self.manual_review = manual_review
        self.attempt_id = attempt_id
        self.calls = []
        self._last = None

    def execute(self, sql, params=None):
        self.calls.append(sql.strip())
        stmt = sql.strip()
        if stmt.startswith("SELECT status FROM applications"):
            self._last = [{"status": self.locked_status}] if self.locked_status is not None else []
        elif stmt.startswith("SELECT outcome, reason, reviewer_name, reviewer_role, reviewed_at "
                              "FROM manual_reviews"):
            self._last = [self.manual_review] if self.manual_review else []
        elif stmt.startswith("SELECT id, (lease_expires_at > now())"):
            self._last = []
        elif stmt.startswith("SELECT state, (lease_expires_at > now()) AS live"):
            self._last = [{"state": "in_progress", "live": True}]
        elif stmt.startswith("INSERT INTO decision_attempts"):
            self._last = [{"id": self.attempt_id}]
        elif stmt.startswith("UPDATE decision_attempts SET state = 'failed'"):
            self._last = None
        else:
            self._last = []

    def fetchall(self):
        return self._last or []


def test_run_decision_rejects_a_response_whose_attempt_id_does_not_match(monkeypatch):
    """Security/correctness fix (PR #6 review): decision-service must
    answer the SAME attempt this request created -- a response carrying a
    different (or missing) attempt_id is never trusted enough to persist
    anything from, and the attempt itself is marked failed so a retry can
    proceed."""
    def _fake_query(sql, params=None):
        if "FROM decisions" in sql:
            return []  # first call, no existing decision -- ownership via access_token
        return [_APPLICATION_ROW]

    monkeypatch.setattr(db, "query", _fake_query)

    cursor = _FakeRunDecisionTxCursor(locked_status="submitted", attempt_id=7)

    @contextlib.contextmanager
    def _fake_tx():
        yield cursor

    monkeypatch.setattr(db, "transaction", _fake_tx)

    def _fake_post(base_url, path, payload, headers=None):
        # Deliberately answers with the WRONG attempt_id.
        return {"outcome": "approve", "score": 700, "reason": None, "attempt_id": 999}

    monkeypatch.setattr(clients, "post", _fake_post)

    resp = client.post(
        "/applications/10/decision",
        json={"access_token": _ACCESS_TOKEN},
    )

    assert resp.status_code == 502
    # TXN B never opens at all on a mismatch -- decision_state.mark_attempt_failed
    # opens its OWN short transaction (yielding this same fake cursor), so the
    # only statement issued after the SELECTs from TXN A is the failure UPDATE --
    # never an INSERT INTO decisions or decision_events.
    assert not any("INSERT INTO decisions" in c for c in cursor.calls)
    assert not any("INSERT INTO decision_events" in c for c in cursor.calls)
    assert any(c.startswith("UPDATE decision_attempts SET state = 'failed'") for c in cursor.calls)
