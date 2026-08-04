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
