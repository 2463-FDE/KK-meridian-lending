import os

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://meridian:postgres@postgres:5432/meridian",
)
# processor key from environment only — no hardcoded fallback. Set via secrets manager.
PROCESSOR_API_KEY = os.getenv("PROCESSOR_API_KEY", "")
PROCESSOR_BASE_URL = os.getenv("PROCESSOR_BASE_URL", "https://api.cardprocessor.example.com")
SETTLEMENT_FILE = os.getenv("SETTLEMENT_FILE", "data/settlement.csv")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Shared secret proving a caller reached a money-moving route through the gateway
# or payment-service rather than merely by being on the compose network. No
# default: a fallback that lives in this repository is not a secret, and the
# scenario this guard exists for -- the network boundary bypassed or a port
# re-exposed -- is exactly the one where an attacker can read it here.
INTERNAL_SERVICE_TOKEN = os.getenv("INTERNAL_SERVICE_TOKEN", "")

# --- startup validation ------------------------------------------------------
#
# Review finding (PR #22, high): the guard was undercut by its own configuration.
# compose started this service with `dev-internal-token-change-me` whenever .env
# was absent, so in the default runtime any caller who could reach
# servicing-service:8002 could read that value out of the repository and move
# money with it. The check ran, passed, and protected nothing.
#
# So a known value is now a boot failure, not a weaker mode of operation.
ENVIRONMENT = os.getenv("ENVIRONMENT", "").lower()
_DEV_ENVIRONMENTS = ("development", "dev", "test", "local")

#: Values published in this repository -- compose defaults past and present, test
#: fixtures. None of them may authenticate a money movement outside dev/test.
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

    An unset ENVIRONMENT is treated as PRODUCTION, not as dev. A container boots
    without one, so "unset" is a real reachable deployment state -- defaulting it
    the other way would make this pass in precisely the case it exists to catch.
    """
    env = (environment if environment is not None else ENVIRONMENT).lower()
    value = token if token is not None else INTERNAL_SERVICE_TOKEN
    if env in _DEV_ENVIRONMENTS:
        return
    if value in KNOWN_DEV_TOKENS:
        raise InsecureInternalTokenError(
            "INTERNAL_SERVICE_TOKEN is empty or set to a value published in this "
            "repository, and ENVIRONMENT is not a development environment "
            f"(ENVIRONMENT={env!r}). servicing-service moves money on these routes; "
            "supply a real secret, or set ENVIRONMENT to one of "
            + ", ".join(_DEV_ENVIRONMENTS) + " for local work."
        )
