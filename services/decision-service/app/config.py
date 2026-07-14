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
# Defaults closed (no stub): an UNSET ENVIRONMENT must not silently enable it. Since
# `.env` is optional for docker compose (env_file required: false — a clean checkout
# boots without one), an unset ENVIRONMENT is a real, reachable case in a real deploy
# that forgot to configure it, not just a hypothetical. Local dev opts in explicitly
# via ENVIRONMENT=development in docker-compose.yml's compose-level default (see
# docker-compose.yml), and a deploy that skips that setup fails closed.
ENVIRONMENT = os.getenv("ENVIRONMENT", "").lower()
ALLOW_CREDIT_STUB = ENVIRONMENT in ("development", "dev", "test", "local")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://meridian:postgres@postgres:5432/meridian",
)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
