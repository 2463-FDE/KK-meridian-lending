"""Decisioning tests for the rules scorecard and the Week 3 AI-scorer wrapper +
reason-code mapping.

Both `decide()` tests use the deterministic stub bureau AND model paths: there is no
live Experian or licensed AI scorer in the test environment, so both calls fall back to
their deterministic stubs. Persistence (both `decisions` and `decision_events`) is
best-effort and swallowed when no DB is present, so these tests exercise the scoring
chain's outcome without a database.
"""
import importlib

import pytest

from app import config, decision
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


def test_unset_environment_and_key_defaults_closed_not_open(monkeypatch):
    """Codex review on PR #3: a deploy that forgets both EXPERIAN_KEY and
    ENVIRONMENT (env_file is optional now -- this is a reachable case, not
    hypothetical) must not silently land in dev-stub mode. Reloads config.py with
    both unset to prove the real os.getenv default resolves closed, not just that
    the already-computed constant does."""
    monkeypatch.delenv("ENVIRONMENT", raising=False)
    monkeypatch.delenv("EXPERIAN_KEY", raising=False)
    importlib.reload(config)
    importlib.reload(decision)
    try:
        assert config.ALLOW_CREDIT_STUB is False
        with pytest.raises(decision.CreditBureauUnavailableError):
            decision._pull_credit("123456782")
    finally:
        # Explicitly restore the test-suite baseline (conftest.py) before reloading --
        # monkeypatch's own teardown only restores env vars *after* this test function
        # returns, so relying on it here would reload the modules while ENVIRONMENT is
        # still unset and leave every later test in this closed state.
        monkeypatch.setenv("ENVIRONMENT", "test")
        importlib.reload(config)
        importlib.reload(decision)


# --- Week 3: the licensed AI scorer has the same fail-closed contract as the bureau
# call above -- a missing/unreachable licensed model must not silently score from
# fake data outside dev/test either.

def test_missing_model_key_stubs_in_dev(monkeypatch):
    monkeypatch.setattr(decision, "AI_MODEL_API_KEY", "")
    monkeypatch.setattr(decision, "ALLOW_MODEL_STUB", True)
    score, model_version = decision._call_ai_scorer(680, {"income": 100000})
    assert score == decision._stub_model_score(680, 100000)
    # A stubbed score must never be recorded as if the real vendor produced it.
    assert model_version.endswith("-stub")


def test_missing_model_key_fails_closed_outside_dev(monkeypatch):
    monkeypatch.setattr(decision, "AI_MODEL_API_KEY", "")
    monkeypatch.setattr(decision, "ALLOW_MODEL_STUB", False)
    # Reference decision.ModelUnavailableError (not a static top-of-file import):
    # test_unset_environment_and_key_defaults_closed_not_open above reloads the
    # `decision` module, which mints a new class object for every exception type
    # it defines -- a statically-imported reference would no longer match instances
    # raised after that reload.
    with pytest.raises(decision.ModelUnavailableError):
        decision._call_ai_scorer(680, {"income": 100000})


# --- Week 3: adverse-action reasons map to whichever input actually drove the score
# down, instead of a fixed "purchasing history" string regardless of applicant.

def test_reason_codes_reflect_low_bureau_score():
    # Poor bureau score, healthy income -> bureau is the larger shortfall.
    reasons = decision._reason_codes(bureau_score=500, income=60000)
    assert reasons == [decision.REASON_LOW_BUREAU_SCORE]


def test_reason_codes_reflect_insufficient_income():
    # Excellent bureau score, zero income -> income is the larger shortfall.
    reasons = decision._reason_codes(bureau_score=800, income=0)
    assert reasons == [decision.REASON_INSUFFICIENT_INCOME]


def test_reason_codes_empty_when_both_healthy():
    reasons = decision._reason_codes(bureau_score=800, income=100000)
    assert reasons == []


def test_decide_returns_empty_reason_codes_on_approve():
    result = decide({"app_id": 3, "ssn": "123456782", "income": 100000})
    assert result["decision"] == "approve"
    assert result["reason_codes"] == []
    assert result["adverse_action_reason"] is None


def test_decide_returns_specific_reason_code_on_deny():
    result = decide({"app_id": 4, "ssn": "123456781", "income": 0})
    assert result["decision"] == "deny"
    assert result["reason_codes"]
    assert result["adverse_action_reason"] == result["reason_codes"][0]
    # Not the old generic nearest-checkbox string.
    assert result["adverse_action_reason"] != "purchasing history"
