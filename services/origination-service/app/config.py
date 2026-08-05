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
