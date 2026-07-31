"""Test baseline environment -- must be set before app.config is first
imported (module-level, not inside a fixture), since config.py reads it once
at import time."""
import os

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("INTERNAL_SERVICE_TOKEN", "test-internal-token")
