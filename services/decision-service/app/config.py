"""Decision service configuration.

Carried over from origination when decisioning was split into its own service.
Bureau credentials and the DB password come from the environment only — no
hardcoded fallback. Set via the secrets manager in every environment.
"""
import os

EXPERIAN_KEY = os.getenv("EXPERIAN_KEY", "")
EXPERIAN_BASE_URL = os.getenv("EXPERIAN_BASE_URL", "https://api.experian.example.com/v2")

CORE_BANKING_API_KEY = os.getenv("CORE_BANKING_API_KEY", "")

# Whether a missing/failed bureau call may fall back to a deterministic stub score.
# Defaults closed (no stub) unless ENVIRONMENT explicitly names a known-safe dev/test
# environment — an unset EXPERIAN_KEY must not silently drive real lending decisions.
ENVIRONMENT = os.getenv("ENVIRONMENT", "development").lower()
ALLOW_CREDIT_STUB = ENVIRONMENT in ("development", "dev", "test", "local")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://meridian:postgres@postgres:5432/meridian",
)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
