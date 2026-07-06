"""Loan Assistant service configuration."""
import os

ORIGINATION_URL = os.getenv("ORIGINATION_URL", "http://origination-service:8001")

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
