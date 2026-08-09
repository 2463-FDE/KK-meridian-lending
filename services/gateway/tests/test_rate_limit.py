"""Tests for the gateway's per-client-IP rate limiter (rate_limit.py).

The gateway had no rate limiting at all before this -- a single caller could
hammer any endpoint, including anonymous /los/* traffic, at no cost.
"""
from app import auth
from app import rate_limit as rate_limit_module
from app.main import app
from fastapi.testclient import TestClient

client = TestClient(app)


class _FakeRedis:
    """In-memory stand-in for the Redis fixed-window counter -- INCR + EXPIRE
    is all rate_limit.py needs."""

    def __init__(self):
        self.counts = {}

    def incr(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    def expire(self, key, seconds):
        pass


def test_requests_under_the_limit_all_succeed(monkeypatch):
    # Same fake instance returned on every call -- the counter must persist
    # across requests within the window, not reset each time.
    fake_redis = _FakeRedis()
    monkeypatch.setattr(auth, "_client", lambda: fake_redis)
    monkeypatch.setattr(rate_limit_module, "RATE_LIMIT_MAX_REQUESTS", 3)

    for _ in range(3):
        resp = client.get("/lss/loans/1")
        assert resp.status_code != 429


def test_request_over_the_limit_is_429(monkeypatch):
    fake_redis = _FakeRedis()
    monkeypatch.setattr(auth, "_client", lambda: fake_redis)
    monkeypatch.setattr(rate_limit_module, "RATE_LIMIT_MAX_REQUESTS", 2)
    monkeypatch.setattr(auth, "get_session", lambda token: None)

    for _ in range(2):
        resp = client.get("/lss/loans/1")
        assert resp.status_code == 401  # unauthenticated, but still counted

    resp = client.get("/lss/loans/1")
    assert resp.status_code == 429


def test_health_endpoint_is_exempt_from_rate_limiting(monkeypatch):
    calls = []

    class _CountingRedis(_FakeRedis):
        def incr(self, key):
            calls.append(key)
            return super().incr(key)

    fake_redis = _CountingRedis()
    monkeypatch.setattr(auth, "_client", lambda: fake_redis)
    monkeypatch.setattr(rate_limit_module, "RATE_LIMIT_MAX_REQUESTS", 1)

    for _ in range(10):
        resp = client.get("/health")
        assert resp.status_code == 200

    assert calls == []


def test_redis_error_fails_open_not_closed(monkeypatch):
    class _BrokenRedis:
        def incr(self, key):
            raise ConnectionError("redis down")

    monkeypatch.setattr(auth, "_client", lambda: _BrokenRedis())
    monkeypatch.setattr(auth, "get_session", lambda token: None)
    monkeypatch.setattr(rate_limit_module, "RATE_LIMIT_MAX_REQUESTS", 1)

    # Must not 429 due to the limiter's own failure -- the request proceeds to
    # normal auth handling (401, no valid session) instead of being blocked by
    # a broken rate limiter.
    resp = client.get("/lss/loans/1")
    assert resp.status_code == 401


# --- the shipped stack must ship the shipped limit ---------------------------

import re  # noqa: E402  -- used only by the compose assertions below
from pathlib import Path  # noqa: E402

_REPO_ROOT = Path(__file__).resolve().parents[3]


def _gateway_env(compose_text: str, key: str):
    """The value Compose would substitute for `key` in the gateway service."""
    match = re.search(rf"^\s*{re.escape(key)}:\s*(.+)$", compose_text, re.MULTILINE)
    return match.group(1).strip() if match else None


def test_the_default_compose_stack_ships_the_real_rate_limit():
    """A control the documented run path overrides is not a control.

    docker-compose.yml used to set `${GATEWAY_RATE_LIMIT_MAX:-2000}`, with a
    comment arguing the shipped control was unweakened because config.py still
    said 120. Compose substitutes its default and exports it, so the variable
    always won: `make up` and `docker compose up` published a gateway allowing
    2,000 requests a minute, and the 120 lived only in a file nothing read.
    Review finding on PR #10.

    Asserted against the compose file itself because that is the artefact that
    was wrong -- the application default was already correct and testing it
    again would have caught nothing.
    """
    compose = (_REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    value = _gateway_env(compose, "RATE_LIMIT_MAX_REQUESTS")
    assert value is not None, "the gateway service no longer sets the limit at all"
    assert "2000" not in value, (
        f"the default compose stack raises the rate limit ({value}) -- put the "
        f"higher value in docker-compose.e2e.yml, which is applied explicitly"
    )
    assert "120" in value, f"expected the shipped 120/60s control, found {value}"


def test_the_browser_suite_overlay_is_where_the_raise_lives():
    """The raise still has to exist somewhere, or the e2e suite trips the limit."""
    overlay = _REPO_ROOT / "docker-compose.e2e.yml"
    assert overlay.is_file(), "docker-compose.e2e.yml is missing"
    assert "2000" in _gateway_env(overlay.read_text(encoding="utf-8"),
                                  "RATE_LIMIT_MAX_REQUESTS")


def test_the_e2e_job_applies_the_overlay():
    """...and the job that needs it must actually apply it."""
    ci = (_REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "docker-compose.e2e.yml" in ci, (
        "the e2e job starts the default stack, whose rate limit the browser "
        "suite will trip"
    )
