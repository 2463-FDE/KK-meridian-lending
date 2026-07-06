"""Decision service configuration.

Carried over from origination when decisioning was split into its own service.
Bureau credentials come from the environment only — no hardcoded fallback. Set
EXPERIAN_KEY / CORE_BANKING_API_KEY via the secrets manager in every environment.
"""
import os

EXPERIAN_KEY = os.getenv("EXPERIAN_KEY", "")
EXPERIAN_BASE_URL = os.getenv("EXPERIAN_BASE_URL", "https://api.experian.example.com/v2")

CORE_BANKING_API_KEY = os.getenv("CORE_BANKING_API_KEY", "")

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://meridian:meridian_dev_pw_2024@postgres:5432/meridian",
)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
