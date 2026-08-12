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


import pytest


@pytest.fixture(autouse=True)
def servicing_reachable(monkeypatch, request):
    """Assume servicing is healthy unless a test says otherwise.

    `charge()` preflights servicing before every `authorize_charge()` and FAILS
    CLOSED (review round 4): a timeout, DNS failure or TLS error refuses the
    charge, because capturing a card while the system that credits the loan is
    unreachable is the outcome this whole guard exists to prevent.

    Under test that call would otherwise hit a real network, fail, and refuse
    every charge -- so the suite would be asserting the outage path everywhere
    and the charge flow nowhere. The default here is "servicing is up"; the
    tests that care about the outage override it, and they are the ones that
    prove the fail-closed behaviour rather than this fixture hiding it.

    Tests that exercise the preflight itself opt out with
    `@pytest.mark.real_servicing_preflight`, so this default can never hide the
    behaviour it stands in for.
    """
    if request.node.get_closest_marker("real_servicing_preflight"):
        return
    monkeypatch.setattr("app.payments._servicing_auth_ok", lambda: True)


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "real_servicing_preflight: run against the real _servicing_auth_ok, not the "
        "healthy-by-default fixture",
    )
