"""Shared fixtures for gateway tests.

Autouse: no test should make a real Redis connection. RateLimitMiddleware now
calls auth._client() on every single request through the app, and outside
Docker the "redis" hostname doesn't resolve -- without this, every existing
test that doesn't itself care about rate limiting would hang/fail on a real
connection attempt. Tests that specifically exercise rate-limit behavior
override this themselves via their own monkeypatch.setattr(auth, "_client", ...).
"""
import pytest

from app import auth


class _NullRateLimitRedis:
    """Never blocks, never errors -- the default for every test that isn't
    specifically testing the rate limiter itself."""

    def incr(self, key):
        return 1

    def expire(self, key, seconds):
        pass


@pytest.fixture(autouse=True)
def _no_real_redis_in_rate_limiter(monkeypatch):
    monkeypatch.setattr(auth, "_client", lambda: _NullRateLimitRedis())
