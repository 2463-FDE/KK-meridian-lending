"""Origination service configuration."""
import os

# Dead: decisioning (and its bureau credentials) moved to decision-service. Nothing
# in this service reads EXPERIAN_KEY / CORE_BANKING_API_KEY anymore.

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://meridian:postgres@postgres:5432/meridian",
)

SERVICING_URL = os.getenv("SERVICING_URL", "http://servicing-service:8002")

# Extracted microservices the LOS now orchestrates over HTTP (formerly in-process:
# CIP/KYC, decisioning, and offer/disclosure). Defaults match the docker network.
KYC_URL = os.getenv("KYC_URL", "http://kyc-service:8003")
DECISION_URL = os.getenv("DECISION_URL", "http://decision-service:8004")
DISCLOSURE_URL = os.getenv("DISCLOSURE_URL", "http://disclosure-service:8005")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Sent as X-Internal-Token on the server-to-server call to decision-service's
# POST /decisions (see clients.py / routers/applications.py) -- must match
# decision-service's own copy (docker-compose.yml wires both from the same var).
INTERNAL_SERVICE_TOKEN = os.getenv("INTERNAL_SERVICE_TOKEN", "")

# How long a freshly-minted accept_token (the one-time link an anonymous,
# no-account borrower uses to accept their own approved offer -- see
# decision_state.issue_accept_token / routers/applications.py accept_offer)
# stays valid before it must be re-requested via a fresh decision call.
#
# Business assumption, not a regulatory figure: this app has no separate
# "review your offer" step today -- accept and board happen as one action
# the instant the borrower clicks through -- so the window only needs to
# cover "how long will a real borrower plausibly sit on the decision screen
# before deciding," not a multi-day disclosure/rescission period. 24 hours
# is a deliberately generous guess for that, configurable per environment;
# tighten or loosen via ACCEPT_TOKEN_TTL_SECONDS without a code change.
ACCEPT_TOKEN_TTL_SECONDS = int(os.getenv("ACCEPT_TOKEN_TTL_SECONDS", str(24 * 60 * 60)))

# --- the note rate an offer is priced at ------------------------------------
#
# **A training default, and not a pricing policy.** No client decision
# authorises risk-based pricing here -- not by score, not by income, not by DTI,
# not by employment, and not by anything a model produces. There is exactly one
# rate, it applies to every offer this system generates, and it exists so the
# demo can produce a Truth-in-Lending disclosure at all.
#
# It lives HERE because the browser was deciding it. `frontend/app/apply` and
# `frontend/app/underwriting` each held `const OFFER_RATE_PCT = 7.99` and posted
# it into offer creation, so the contractual note rate on a real loan was
# whatever the client sent -- with the same number written down in four more
# places (both frontends, this service's OfferIn default, disclosure-service's).
# Five copies of a contractual term is five chances to disagree, and the one that
# reaches the borrower's loan was the one furthest from any authority.
#
# Environment-overridable so a demo can be run at a different rate without a code
# change. Seeded and historical loans are NOT affected: they carry the rate they
# were boarded with (`loans.note_rate_pct`), and the bulk synthetic portfolio
# deliberately holds a spread of rates. This is the rate for offers made from now
# on, not a normalisation of what already exists.
DEMO_NOTE_RATE_PCT = float(os.getenv("DEMO_NOTE_RATE_PCT", "7.99"))

#: What the pricing endpoint tells a caller about where the figure came from, so
#: nobody reads it as an underwritten, per-applicant rate.
NOTE_RATE_SOURCE = "training_default"

# How long the SUBMISSION token (ApplicationCreated.access_token) stays valid.
# It proves ownership for the very first decision call on an application whose
# borrower has no account yet -- the same bearer-credential-at-rest problem the
# acceptance token had, so it gets the same hash/expiry/single-use lifecycle
# (see decision_state.issue_access_token). 24 hours by design symmetry with the
# acceptance window above: a borrower who submits and comes back the next day
# still gets their first decision; anything older re-submits. Configurable per
# environment.
ACCESS_TOKEN_TTL_SECONDS = int(os.getenv("ACCESS_TOKEN_TTL_SECONDS", str(24 * 60 * 60)))

# PR #6 review (Finding 2): how long a decision_attempts row may sit
# 'in_progress' before a later request is allowed to treat it as abandoned
# (a crashed process) and atomically recover -- see
# decision_state.start_decision_attempt. Must outlive clients.py's own 30s
# timeout on the call to decision-service (a genuinely still-running call
# can legitimately take that full 30s before origination itself gives up) --
# 2x that timeout leaves headroom for the lock/transaction overhead around
# it without leaving a truly crashed attempt blocking reruns any longer than
# necessary on this synchronous, user-facing flow.
DECISION_ATTEMPT_LEASE_SECONDS = int(os.getenv("DECISION_ATTEMPT_LEASE_SECONDS", "60"))


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
    hint = ("origination-service authors applications and decisions, so this token is what proves a request came from inside the "
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
