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
