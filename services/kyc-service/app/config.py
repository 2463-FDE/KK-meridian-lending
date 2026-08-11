"""KYC service configuration."""
import os

# Dead: nothing in this service reads EXPERIAN_KEY/EXPERIAN_BASE_URL (KYC-service
# never called Experian; this was leftover from the pre-decomposition config template).

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://meridian:postgres@postgres:5432/meridian",
)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Shared secret proving a caller reached this service through the gateway or
# origination-service rather than straight off the host. Defaults to empty on
# purpose: routers/kyc.py compares with `not TOKEN or header != TOKEN`, so an
# unset value can never match and a deploy that forgets to set one fails closed.
# Same contract as decision/disclosure/payment/origination-service.
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
