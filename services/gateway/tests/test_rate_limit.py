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
