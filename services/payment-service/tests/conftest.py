"""Test baseline environment -- must be set before app.config is first
imported (module-level, not inside a fixture), since config.py reads it once
at import time."""
import os

os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("INTERNAL_SERVICE_TOKEN", "test-internal-token")
# The in-process reconciler is off by default under test: a background drain
# polling a real database while unit tests run is nondeterminism nobody asked
# for. test_reconciler_lifecycle.py turns it back on explicitly.
os.environ.setdefault("PAYMENT_RECONCILE_INTERVAL_SECONDS", "0")
