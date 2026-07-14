"""Readiness regression: a missing EXPERIAN_KEY outside dev/test must fail
/health, not surface as a 500 on the first real POST /decisions.

Before this fix, docker-compose.yml made decision-service's .env optional but
supplied no ENVIRONMENT/EXPERIAN_KEY default: a clean checkout booted with
/health returning ok while every decision request raised
CreditBureauUnavailableError. main.py now computes readiness from the same
config ALLOW_CREDIT_STUB/EXPERIAN_KEY that decision.py fails closed on
(see test_decision.py's test_unset_environment_and_key_defaults_closed_not_open).
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
    main = _reload_main()
    try:
        client = TestClient(main.app)
        resp = client.get("/health")
        assert resp.status_code == 200
    finally:
        monkeypatch.setenv("ENVIRONMENT", "test")
        _reload_main()
