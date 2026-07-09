"""Test baseline environment.

config.py now defaults ENVIRONMENT to closed (no dev-stub fallback) since an unset
ENVIRONMENT is a real, reachable production misconfiguration, not just a test
convenience gap. The test suite needs its own explicit dev/test environment rather
than depending on whatever happens to be set in the shell running pytest -- this
must be set before app.config is first imported (module-level, not inside a
fixture), since config.py reads it once at import time.
"""
import os

os.environ.setdefault("ENVIRONMENT", "test")
