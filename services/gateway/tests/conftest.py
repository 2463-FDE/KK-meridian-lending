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


@pytest.fixture(autouse=True)
def _principal_signing_key_for_tests(monkeypatch):
    """A throwaway Ed25519 key, generated per test.

    The gateway now mints a signed principal on every money-moving hop, so those
    routes cannot complete without one -- and refusing with 503 rather than
    proxying an unsigned request is deliberate: a money route that cannot say who
    is acting must fail closed. The tests exercising the authorization tiers
    therefore need a real key.

    Generated, never committed. A fixture holding a fixed private key would put
    an admin-minting credential in the repository, which is what
    `test_principal_signing.py::test_no_key_material_is_committed` forbids.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    from app import config, principal

    pem = Ed25519PrivateKey.generate().private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode()
    monkeypatch.setattr(config, "PRINCIPAL_SIGNING_KEY", pem)
    monkeypatch.setattr(principal, "PRINCIPAL_SIGNING_KEY", pem)
