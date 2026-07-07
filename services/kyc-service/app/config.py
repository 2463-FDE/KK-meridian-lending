"""KYC service configuration."""
import os

# Dead: nothing in this service reads EXPERIAN_KEY/EXPERIAN_BASE_URL (KYC-service
# never called Experian; this was leftover from the pre-decomposition config template).

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://meridian:@postgres:5432/meridian",
)

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
