"""Decisioning tests for the rules scorecard (these PASS).

Both tests use the deterministic stub bureau path: there is no live Experian in the test
environment, so `_pull_credit` falls back to its deterministic stub (680 for an SSN ending
in an even digit, 612 otherwise). Persistence is best-effort and swallowed when no DB is
present, so these tests exercise the scorecard outcome without a database.

NOTE (intentional debt, left UNTESTED): there is deliberately NO test asserting that a
decision audit trail / reason-code accuracy exists. The `decisions` table stores OUTCOME
ONLY (no reason, no model drivers, no inputs, no timestamp), and the adverse-action reason
is a generic nearest-checkbox string — that debt (D4, D10, twists #1/#2) stays untested.
"""
import pytest

from app import decision
from app.decision import CreditBureauUnavailableError, decide


def test_clear_approve():
    # SSN ends in an even digit -> stub bureau score 680; high income clears the scorecard.
    result = decide({"app_id": 1, "ssn": "123456782", "income": 100000})
    assert result["decision"] == "approve"
    assert result["score"] >= 660


def test_clear_deny():
    # SSN ends in an odd digit -> stub bureau score 612; zero income sinks the scorecard.
    result = decide({"app_id": 2, "ssn": "123456781", "income": 0})
    assert result["decision"] == "deny"
    assert result["score"] < 600


# Regression (Codex review on PR #3): dropping the hardcoded EXPERIAN_KEY fallback
# means the key can now legitimately be empty. Outside dev/test that must fail the
# decision request, not silently approve/deny from a fake stub score.
def test_missing_bureau_key_stubs_in_dev(monkeypatch):
    monkeypatch.setattr(decision, "EXPERIAN_KEY", "")
    monkeypatch.setattr(decision, "ALLOW_CREDIT_STUB", True)
    # must not raise — dev/test is allowed to fall back to the deterministic stub
    score = decision._pull_credit("123456782")
    assert score == 680


def test_missing_bureau_key_fails_closed_outside_dev(monkeypatch):
    monkeypatch.setattr(decision, "EXPERIAN_KEY", "")
    monkeypatch.setattr(decision, "ALLOW_CREDIT_STUB", False)
    with pytest.raises(CreditBureauUnavailableError):
        decision._pull_credit("123456782")
