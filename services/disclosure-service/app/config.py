"""Disclosure service configuration."""
import os

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://meridian:postgres@postgres:5432/meridian",
)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Required on every POST /offers (see routers/offers.py) -- defense in depth
# for the network boundary (disclosure-service has no host port mapping; see
# docker-compose.yml). Unset means no caller can ever match it, so a deploy
# that forgets to configure this fails closed rather than open.
INTERNAL_SERVICE_TOKEN = os.getenv("INTERNAL_SERVICE_TOKEN", "")
