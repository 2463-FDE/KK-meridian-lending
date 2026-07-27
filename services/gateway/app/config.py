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
