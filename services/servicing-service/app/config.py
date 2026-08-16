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

    An unset ENVIRONMENT is treated as PRODUCTION, not as dev. A container boots
    without one, so "unset" is a real reachable deployment state -- defaulting it
    the other way would make this pass in precisely the case it exists to catch.
    """
    env = (environment if environment is not None else ENVIRONMENT).lower()
    value = token if token is not None else INTERNAL_SERVICE_TOKEN
    if env in _DEV_ENVIRONMENTS:
        return

    hint = ("servicing-service moves money on these routes and they have no role "
            "or ownership check of their own, so this token is the whole "
            "application-level defence once the network boundary fails. Generate "
            "one with: python -c \"import secrets; print(secrets.token_urlsafe(32))\" "
            "-- or set ENVIRONMENT to one of " + ", ".join(_DEV_ENVIRONMENTS) +
            " for local work.")

    if value in KNOWN_DEV_TOKENS:
        raise InsecureInternalTokenError(
            "INTERNAL_SERVICE_TOKEN is empty or set to a value published in this "
            f"repository, and ENVIRONMENT is not a development environment "
            f"(ENVIRONMENT={env!r}). " + hint
        )

    # Review round 2 (high): a denylist alone let INTERNAL_SERVICE_TOKEN=1 boot
    # cleanly. Because these routes carry no role or ownership check, a guessable
    # token makes direct money movement brute-forceable the moment the network
    # boundary fails -- which is the only scenario this token exists for. A
    # denylist can only ever reject the weak values someone thought of.
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
# The PUBLIC half of the gateway's signing pair. This service verifies human
# principals and cannot mint one -- that asymmetry is the control. If this value
# were ever the private key, every service holding it could forge an admin, which
# is precisely the shared-secret weakness the signature replaces.
#
# No default: an absent key must fail closed at the money routes, not silently
# admit unverified callers.
PRINCIPAL_VERIFY_KEY = _pem_from_env(os.getenv("PRINCIPAL_VERIFY_KEY", ""))

#: Who a valid assertion must come from, and who it must be addressed to. An
#: assertion minted for another audience is refused here even though its
#: signature is perfectly good -- that is what stops one captured off a different
#: hop being replayed at this one.
ASSERTION_ISSUER = os.getenv("ASSERTION_ISSUER", "meridian-gateway")
ASSERTION_AUDIENCE = os.getenv("ASSERTION_AUDIENCE", "servicing-service")

#: Ceiling on `exp - iat`, checked independently of expiry. Expiry alone only
#: asks "is this still valid?"; this asks "was it ever allowed to be valid this
#: long?" -- which is what catches a gateway misconfigured to a week-long TTL, or
#: a leaked key being used to mint long-lived assertions.
ASSERTION_MAX_LIFETIME_SECONDS = int(os.getenv("ASSERTION_MAX_LIFETIME_SECONDS", "300"))


class PrincipalKeyError(RuntimeError):
    """Raised at startup when PRINCIPAL_VERIFY_KEY is present but unusable."""


def validate_principal_verify_key(environment: str | None = None,
                                  key_pem: str | None = None) -> None:
    """Refuse to start on a missing or unusable verification key.

    Mirrors `validate_internal_token`, including its treatment of an unset
    ENVIRONMENT as production. A dev environment may run without a key: the
    money routes then refuse every request for want of a verifiable principal,
    which is loud and local. What no environment may do is boot with a key that
    does not parse, or with a PRIVATE key here -- the first fails at request
    time on a staff action, and the second silently restores the ability of this
    service to mint the identities it is supposed to only check.
    """
    env = (environment if environment is not None else ENVIRONMENT).lower()
    value = key_pem if key_pem is not None else PRINCIPAL_VERIFY_KEY
    hint = (
        "Generate a pair with `python db/tools/generate_principal_keypair.py`. "
        "PRINCIPAL_VERIFY_KEY is the PUBLIC half; the private half belongs to the "
        "gateway alone."
    )

    if value and "PRIVATE KEY" in value:
        raise PrincipalKeyError(
            "PRINCIPAL_VERIFY_KEY holds a PRIVATE key. This service must not be "
            "able to mint a principal -- only the gateway may. " + hint
        )

    if not value:
        if env in _DEV_ENVIRONMENTS:
            return
        raise PrincipalKeyError(
            "PRINCIPAL_VERIFY_KEY is unset and ENVIRONMENT is not a development "
            f"environment (ENVIRONMENT={env!r}). Money-moving routes cannot verify "
            f"who is acting. " + hint
        )

    from cryptography.hazmat.primitives import serialization

    try:
        key = serialization.load_pem_public_key(value.encode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise PrincipalKeyError(
            f"PRINCIPAL_VERIFY_KEY is not a readable PEM public key "
            f"({type(exc).__name__}). " + hint
        ) from exc
    if key.__class__.__name__ != "Ed25519PublicKey":
        raise PrincipalKeyError(
            f"PRINCIPAL_VERIFY_KEY is a {key.__class__.__name__}; Ed25519 is "
            f"required so the verifier can pin a single algorithm. " + hint
        )
