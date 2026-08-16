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
    hint = ("the gateway signs every internal call it forwards, so this token is what proves a request came from inside the "
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


def _pem_from_env(raw: str) -> str:
    """A PEM as it survives an environment variable.

    `.env` and `docker compose` are line-based, so a multi-line PEM is written
    with its newlines escaped -- and NOTHING decodes an escape on the way back
    out. `os.getenv` hands over the literal two-character sequence, which is not
    a PEM, and `load_pem_*` refuses it with `InvalidByte(0, 92)` -- byte 92 being
    the backslash.

    This function is that decode. It was missing while three comments in this
    change claimed it existed: bootstrap wrote escaped keys, both services read
    them raw, and every money route would have refused with a 401 that looks like
    an authorization problem rather than a configuration one. Caught by running
    the documented bootstrap and trying to load what it wrote.

    Both shapes are accepted, because both are real: a secrets manager or a
    mounted file supplies genuine newlines, and `.env` supplies escapes.
    """
    return raw.replace("\\n", "\n") if "\\n" in raw else raw


# --- signed human principal (spec 0002 REQ-ID-3) ------------------------------
#
# The gateway is the only component that turns a Redis session into a person, so
# it is the only one that can honestly say who is acting. It signs that statement
# with a key nothing else holds; every other service verifies with the public
# half and therefore cannot forge one. See app/principal.py.
#
# No default, for the same reason INTERNAL_SERVICE_TOKEN has none: a key
# committed here is not a key. Unlike the shared token, though, a *weak* value is
# not the risk -- a malformed one is, because it fails at mint time, on a staff
# request, in production. So it is parsed at boot rather than trusted.
PRINCIPAL_SIGNING_KEY = _pem_from_env(os.getenv("PRINCIPAL_SIGNING_KEY", ""))

#: Who the assertion is from, and who it is for. Both are checked by the
#: verifier: an assertion minted for one service must not be replayable at
#: another, which is the whole reason it carries an audience.
ASSERTION_ISSUER = os.getenv("ASSERTION_ISSUER", "meridian-gateway")
ASSERTION_AUDIENCE_SERVICING = "servicing-service"

#: Seconds. Long enough to survive a slow proxied hop, short enough that a
#: captured assertion is worthless before anyone could use it. Minted per
#: request -- this is not a session, and lengthening it to avoid re-minting
#: would turn it into one.
ASSERTION_TTL_SECONDS = int(os.getenv("ASSERTION_TTL_SECONDS", "120"))


class PrincipalKeyError(RuntimeError):
    """Raised at startup when PRINCIPAL_SIGNING_KEY is missing or unusable."""


def validate_principal_signing_key(environment: str | None = None,
                                   key_pem: str | None = None) -> None:
    """Refuse to start without a usable Ed25519 private key.

    Same fail-closed shape as `validate_internal_token`, and same treatment of an
    unset ENVIRONMENT: a container boots without one, so "unset" is production.

    A dev environment may run without a key, and that is deliberate -- most of
    this repo's local work never touches a money route, and requiring key
    generation to run the app would be answered by pasting a key into the
    repository, which is the outcome this rule exists to prevent. What a dev
    environment may NOT do is mint an unsigned or half-signed assertion: with no
    key, minting raises and the money routes refuse, so the failure is loud and
    local rather than silent and shipped.
    """
    env = (environment if environment is not None else ENVIRONMENT).lower()
    value = key_pem if key_pem is not None else PRINCIPAL_SIGNING_KEY
    hint = (
        "Generate a pair with `python db/tools/generate_principal_keypair.py`, "
        "put PRINCIPAL_SIGNING_KEY in the gateway's environment and "
        "PRINCIPAL_VERIFY_KEY in servicing's. The private half must never reach "
        "another service -- if it does, every service can mint a human again and "
        "the signature proves nothing."
    )
    if not value:
        if env in _DEV_ENVIRONMENTS:
            return
        raise PrincipalKeyError(
            "PRINCIPAL_SIGNING_KEY is unset and ENVIRONMENT is not a development "
            f"environment (ENVIRONMENT={env!r}). " + hint
        )

    # Present-but-broken is checked in every environment, dev included: a key
    # that fails to parse fails at mint time, which is a staff request in
    # production and a mystery 500 locally.
    from cryptography.hazmat.primitives import serialization

    try:
        key = serialization.load_pem_private_key(value.encode("utf-8"), password=None)
    except Exception as exc:  # noqa: BLE001
        raise PrincipalKeyError(
            f"PRINCIPAL_SIGNING_KEY is not a readable PEM private key "
            f"({type(exc).__name__}). " + hint
        ) from exc
    if key.__class__.__name__ != "Ed25519PrivateKey":
        raise PrincipalKeyError(
            f"PRINCIPAL_SIGNING_KEY is a {key.__class__.__name__}; Ed25519 is "
            f"required so the verifier can pin a single algorithm. " + hint
        )
