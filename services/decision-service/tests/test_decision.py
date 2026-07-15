"""Decisioning tests for the rules scorecard and the Week 3 AI-scorer wrapper +
reason-code mapping.

Both `decide()` tests use the deterministic stub bureau AND model paths: there is no
live Experian or licensed AI scorer in the test environment, so both calls fall back to
their deterministic stubs.

Persistence used to be best-effort and swallowed when no DB was present -- that was
itself the review finding this PR fixes (a decision could be returned with no
matching audit row). `decide()` now requires its `decisions` + `decision_events`
transaction to succeed. The `_stub_persistence` fixture below stubs `db.transaction`
to succeed by default so the scoring-chain tests below don't need a live Postgres;
`test_decide_raises_when_audit_persistence_fails` overrides that stub to prove
`decide()` actually fails closed when persistence breaks.
"""
import importlib

import pytest

from app import config, decision
from app.decision import CreditBureauUnavailableError, decide


@pytest.fixture(autouse=True)
def _stub_persistence(monkeypatch):
    monkeypatch.setattr(decision.db, "transaction", lambda statements: [[], []])


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
    scored = decision._call_ai_scorer(680, {"income": 100000})
    assert scored["score"] == decision._stub_model_score(680, 100000)
    # A stubbed score must never be recorded as if the real vendor produced it.
    assert scored["model_version"].endswith("-stub")
    # The stub IS the bureau/income formula, so the heuristic is authoritative here.
    assert scored["reason_codes"] == decision._reason_codes(680, 100000)


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


# Review finding: a real vendor response's score alone isn't enough -- the licensed
# model also sees requested_amount/term_months, which the legacy bureau/income
# reason-code formula knows nothing about. A response missing reason_codes must
# fail closed rather than let _run_model() guess from that formula.
def test_real_scorer_response_missing_reason_codes_fails_closed(monkeypatch):
    monkeypatch.setattr(decision, "AI_MODEL_API_KEY", "present-for-this-test")

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"score": 550}  # no reason_codes key at all

    monkeypatch.setattr(decision.httpx, "post", lambda *a, **k: _FakeResponse())
    with pytest.raises(decision.ModelUnavailableError):
        decision._call_ai_scorer(680, {"income": 30000})


def test_real_scorer_response_with_reason_codes_is_used_verbatim(monkeypatch):
    monkeypatch.setattr(decision, "AI_MODEL_API_KEY", "present-for-this-test")

    class _FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {"score": 550, "reason_codes": ["high_debt_to_income"]}

    monkeypatch.setattr(decision.httpx, "post", lambda *a, **k: _FakeResponse())
    scored = decision._call_ai_scorer(680, {"income": 30000})
    assert scored["score"] == 550
    assert scored["reason_codes"] == ["high_debt_to_income"]
    assert not scored["model_version"].endswith("-stub")


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


# --- Review finding: decisions + decision_events must commit or fail together --
# a decision was previously returned to the caller even when its audit row failed
# to write, since each insert was wrapped in its own try/except that only logged.

def test_decide_raises_when_audit_persistence_fails(monkeypatch):
    def _boom(statements):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(decision.db, "transaction", _boom)
    with pytest.raises(decision.DecisionPersistenceError):
        decide({"app_id": 5, "ssn": "123456782", "income": 100000})


def test_decide_persists_decision_and_event_in_one_transaction_call(monkeypatch):
    """Both rows must go through the SAME db.transaction() call (one atomic
    commit/rollback), not two separate db.query() calls that could partially
    succeed."""
    calls = []
    monkeypatch.setattr(
        decision.db, "transaction",
        lambda statements: calls.append(statements) or [[], []],
    )
    decide({"app_id": 6, "ssn": "123456782", "income": 100000})
    assert len(calls) == 1
    statements = calls[0]
    assert len(statements) == 2
    assert "INSERT INTO decisions" in statements[0][0]
    assert "INSERT INTO decision_events" in statements[1][0]
