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

# --- sanctions screening (spec 0004 §4, ADR 0012) ---------------------------
#
# No provider is selected: SCREENING_BASE_URL is a placeholder host and nothing
# in this repository calls a real screening vendor. These exist so the seam in
# app/screening.py has a configured shape rather than a hardcoded one, and so a
# future integration is a provider implementation instead of a redesign.
SCREENING_BASE_URL = os.getenv("SCREENING_BASE_URL", "https://screening.invalid")
SCREENING_KEY = os.getenv("SCREENING_KEY", "")

# A stub outside a development environment is a configuration error, not a
# fallback -- the same gate and the same reasoning as decision-service's
# ALLOW_MODEL_STUB. An unset ENVIRONMENT counts as production: a container boots
# without one, so a deploy that configured no provider must fail at the first
# screen rather than quietly clear everybody.
ALLOW_SCREENING_STUB = os.getenv("ENVIRONMENT", "").lower() in _DEV_ENVIRONMENTS

#: Values that exist in this repository (compose defaults, test fixtures) and so
#: must never authenticate anything outside a dev/test environment.
KNOWN_DEV_TOKENS = frozenset({
    "",
    "dev-internal-token-change-me",
    "test-internal-token",
    "changeme",
})


#: `secrets.token_urlsafe(32)` yields 43 characters; 32 is a floor that admits a
#: hand-rolled secret while rejecting anything guessable by hand.
MIN_TOKEN_LENGTH = 32

#: Substrings that mean "someone typed a word instead of generating a secret".
#: Matched case-insensitively and anywhere in the value, so `MyChangeMe123...`
#: padded out to the length floor is still refused.
PLACEHOLDER_PATTERNS = (
    "changeme", "change-me", "change_me", "password", "passwd", "secret",
    "placeholder", "example", "sample", "dummy", "insecure", "notasecret",
    "todo", "fixme", "xxxx", "1234", "abcd", "test-token", "dev-token",
)


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
    hint = ("kyc-service verifies identity and writes the CIP record, so this token is what proves a request came from inside the "
            "estate. Generate one with: python -c \"import secrets; "
            "print(secrets.token_urlsafe(32))\" -- or set ENVIRONMENT to one of "
            + ", ".join(_DEV_ENVIRONMENTS) + " for local work.")

    if value in KNOWN_DEV_TOKENS:
        raise InsecureInternalTokenError(
            "INTERNAL_SERVICE_TOKEN is empty or set to a value published in this "
            f"repository, and ENVIRONMENT is not a development environment "
            f"(ENVIRONMENT={env!r}). " + hint
        )

    # A denylist can only ever reject the weak values someone thought of, so
    # INTERNAL_SERVICE_TOKEN=1 used to boot cleanly. Reviewed on PR #22 and
    # applied here too: the validator is duplicated across services (no shared
    # library in this repo) and a policy that holds in one of them and not the
    # others is not a policy.
    if len(value) < MIN_TOKEN_LENGTH:
        raise InsecureInternalTokenError(
            f"INTERNAL_SERVICE_TOKEN is {len(value)} characters; at least "
            f"{MIN_TOKEN_LENGTH} are required outside a development environment. " + hint
        )

    lowered = value.lower()
    for pattern in PLACEHOLDER_PATTERNS:
        if pattern in lowered:
            raise InsecureInternalTokenError(
                f"INTERNAL_SERVICE_TOKEN contains the placeholder {pattern!r}, so it "
                "is a description of a secret rather than one. " + hint
            )
