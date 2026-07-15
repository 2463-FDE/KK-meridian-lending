"""Readiness regression: a missing EXPERIAN_KEY or AI_MODEL_API_KEY outside dev/test
must fail /health, not surface as a 500 on the first real POST /decisions.

Before this fix, docker-compose.yml made decision-service's .env optional but
supplied no ENVIRONMENT/EXPERIAN_KEY default: a clean checkout booted with
/health returning ok while every decision request raised
CreditBureauUnavailableError. main.py now computes readiness from the same
config ALLOW_CREDIT_STUB/EXPERIAN_KEY that decision.py fails closed on
(see test_decision.py's test_unset_environment_and_key_defaults_closed_not_open).
Week 3 extends the same readiness check to AI_MODEL_API_KEY, the licensed AI
scorer's own fail-closed dependency.

Review finding: a persistent-volume deployment created before Week 3 never picks
up the new `decision_events` table on its own (db/init only runs on a fresh
volume) -- /health now also checks the table actually exists
(`_decision_events_ready()`), stubbed True/False below rather than requiring a
live Postgres in these tests (this test suite runs without one; see
test_decision.py's own docstring).
"""
import importlib

from fastapi.testclient import TestClient

from app import config


def _reload_main():
    from app import main
    importlib.reload(config)
    importlib.reload(main)
    return main


def test_health_reports_unhealthy_when_key_missing_outside_dev(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("EXPERIAN_KEY", raising=False)
    main = _reload_main()
    monkeypatch.setattr(main, "_decision_events_ready", lambda: True)
    try:
        client = TestClient(main.app)
        resp = client.get("/health")
        assert resp.status_code == 503
    finally:
        monkeypatch.setenv("ENVIRONMENT", "test")
        _reload_main()


def test_health_reports_ok_when_dev_stub_allowed(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.delenv("EXPERIAN_KEY", raising=False)
    monkeypatch.delenv("AI_MODEL_API_KEY", raising=False)
    main = _reload_main()
    monkeypatch.setattr(main, "_decision_events_ready", lambda: True)
    try:
        client = TestClient(main.app)
        resp = client.get("/health")
        assert resp.status_code == 200
    finally:
        monkeypatch.setenv("ENVIRONMENT", "test")
        _reload_main()


def test_health_reports_unhealthy_when_only_model_key_missing_outside_dev(monkeypatch):
    # Bureau key present, AI scorer key absent -- readiness must still catch it.
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("EXPERIAN_KEY", "present-for-this-test")
    monkeypatch.delenv("AI_MODEL_API_KEY", raising=False)
    main = _reload_main()
    monkeypatch.setattr(main, "_decision_events_ready", lambda: True)
    try:
        client = TestClient(main.app)
        resp = client.get("/health")
        assert resp.status_code == 503
        assert "AI_MODEL_API_KEY" in resp.json()["reason"]
    finally:
        monkeypatch.delenv("EXPERIAN_KEY", raising=False)
        monkeypatch.setenv("ENVIRONMENT", "test")
        _reload_main()


def test_health_reports_ok_when_both_keys_present_outside_dev(monkeypatch):
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("EXPERIAN_KEY", "present-for-this-test")
    monkeypatch.setenv("AI_MODEL_API_KEY", "present-for-this-test")
    main = _reload_main()
    monkeypatch.setattr(main, "_decision_events_ready", lambda: True)
    try:
        client = TestClient(main.app)
        resp = client.get("/health")
        assert resp.status_code == 200
    finally:
        monkeypatch.delenv("EXPERIAN_KEY", raising=False)
        monkeypatch.delenv("AI_MODEL_API_KEY", raising=False)
        monkeypatch.setenv("ENVIRONMENT", "test")
        _reload_main()


def test_health_reports_unhealthy_when_decision_events_table_missing(monkeypatch):
    # Keys fine, but the decision_events table itself isn't there yet (e.g. an
    # existing persistent-volume deployment that hasn't applied
    # db/migrations/0004_add_decision_events.sql).
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("EXPERIAN_KEY", "present-for-this-test")
    monkeypatch.setenv("AI_MODEL_API_KEY", "present-for-this-test")
    main = _reload_main()
    monkeypatch.setattr(main, "_decision_events_ready", lambda: False)
    try:
        client = TestClient(main.app)
        resp = client.get("/health")
        assert resp.status_code == 503
        assert "decision_events" in resp.json()["reason"]
    finally:
        monkeypatch.delenv("EXPERIAN_KEY", raising=False)
        monkeypatch.delenv("AI_MODEL_API_KEY", raising=False)
        monkeypatch.setenv("ENVIRONMENT", "test")
        _reload_main()


def test_decision_events_ready_reports_false_when_db_unreachable(monkeypatch):
    """The real (unmocked) _decision_events_ready() must fail closed -- not raise --
    when the DB itself can't be reached, e.g. Postgres down."""
    main = _reload_main()

    def _boom(sql, params=None):
        raise RuntimeError("simulated connection failure")

    monkeypatch.setattr(main.db, "query", _boom)
    assert main._decision_events_ready() is False
