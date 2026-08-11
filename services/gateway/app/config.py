"""Gateway configuration."""
import os

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://meridian:postgres@postgres:5432/meridian",
)
REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

ORIGINATION_URL = os.getenv("ORIGINATION_URL", "http://origination-service:8001")
SERVICING_URL = os.getenv("SERVICING_URL", "http://servicing-service:8002")
KYC_URL = os.getenv("KYC_URL", "http://kyc-service:8003")
DECISION_URL = os.getenv("DECISION_URL", "http://decision-service:8004")
DISCLOSURE_URL = os.getenv("DISCLOSURE_URL", "http://disclosure-service:8005")
PAYMENT_URL = os.getenv("PAYMENT_URL", "http://payment-service:8006")
LOAN_ASSISTANT_URL = os.getenv("LOAN_ASSISTANT_URL", "http://loan-assistant:8007")

# 8-hour sessions. (No refresh, no rotation, no CSRF token — Halcyon "v1 auth".)
SESSION_TTL_SECONDS = int(os.getenv("SESSION_TTL_SECONDS", "28800"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Per-client-IP fixed-window rate limit -- the gateway is the single front door
# for every request into the platform, including anonymous /los/* traffic, and
# had no rate limiting of any kind before this.
RATE_LIMIT_MAX_REQUESTS = int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "120"))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "60"))

# Sent as X-Internal-Token on the staff-ops /decision/* proxy (see main.py) --
# must match decision-service's own copy (docker-compose.yml wires both from
# the same var).
INTERNAL_SERVICE_TOKEN = os.getenv("INTERNAL_SERVICE_TOKEN", "")


# --- internal-token startup validation --------------------------------------
#
# Review finding (PR #18): an empty or skewed INTERNAL_SERVICE_TOKEN previously
# surfaced only per-request, as a 401 that the caller logged as a warning. That
# turns a deployment mistake into a silent, per-applicant outage instead of a
# loud failure at boot. It is checked once, here, at import time.
#
# Known-default values are rejected as hard as an empty one. A secret that ships
# in the repository is not a secret: anyone reading docker-compose.yml has it, so
# accepting it outside a dev box would make every check below theatre.
ENVIRONMENT = os.getenv("ENVIRONMENT", "").lower()
_DEV_ENVIRONMENTS = ("development", "dev", "test", "local")

#: Values that exist in this repository (compose defaults, test fixtures) and so
#: must never authenticate anything outside a dev/test environment.
KNOWN_DEV_TOKENS = frozenset({
    "",
    "dev-internal-token-change-me",
    "test-internal-token",
    "changeme",
})


class InsecureInternalTokenError(RuntimeError):
    """Raised at startup when INTERNAL_SERVICE_TOKEN is unusable."""


def validate_internal_token(environment: str | None = None, token: str | None = None) -> None:
    """Refuse to start on an empty, missing or repository-known token.

    Deliberately NOT skipped when the value merely looks unset -- an unset
    ENVIRONMENT is a real, reachable production state (the container boots
    without one), so it is treated as production and fails closed. Only an
    explicit dev/test environment may run on a known-default token.
    """
    env = (environment if environment is not None else ENVIRONMENT).lower()
    value = token if token is not None else INTERNAL_SERVICE_TOKEN
    if env in _DEV_ENVIRONMENTS:
        return
    if value in KNOWN_DEV_TOKENS:
        raise InsecureInternalTokenError(
            "INTERNAL_SERVICE_TOKEN is empty or set to a value published in this "
            "repository, and ENVIRONMENT is not a development environment "
            f"(ENVIRONMENT={env!r}). Supply a real secret, or set ENVIRONMENT to "
            "one of " + ", ".join(_DEV_ENVIRONMENTS) + " for local work."
        )
