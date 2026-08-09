"""No test in this service may make a real BLS request.

`MACRO_ENABLED` defaults to on, because a deployed loan-assistant should have
the signal. CI's backend job sets only `DATABASE_URL`, so a plain `pytest` run
inherited that default -- and `tests/test_llm_client.py` calls
`summarize_application()` without stubbing `current_signal`, so the suite issued
live requests against a v1 endpoint that allows 25 per day per IP. It stayed
green either way: the provider swallows its own failures by design. A quota
spent by a test run, or a suite that waits on someone else's network, is not
something a passing check would ever have told us about (reviewed on PR #13).

Two layers, because either alone can be bypassed:

  1. the environment variable is set BEFORE `app.config` is imported, since it
     is read once at import time into a module constant;
  2. an autouse fixture asserts, per test, that nothing has re-enabled it and
     that no HTTP client is reachable from the macro module's fetch path.

Anything that genuinely needs to exercise the fetch code does so against a
stubbed transport (see tests/test_macro_signal.py), never against BLS.
"""
import os

import pytest

# Layer 1. Must precede any `from app...` import: app/config.py reads this into
# MACRO_ENABLED at module scope, so setting it inside a fixture would be too
# late for a module already imported by collection.
os.environ["MACRO_ENABLED"] = "0"

from app import config, macro  # noqa: E402


def _no_network(*args, **kwargs):
    raise AssertionError(
        "a test attempted a real outbound macro request -- stub the transport "
        "(see tests/test_macro_signal.py) rather than calling BLS"
    )


@pytest.fixture(autouse=True)
def macro_stays_disabled(monkeypatch):
    """Layer 2. A test that flips the flag must not leak it into the next one.

    Patched on `macro`, not on `config`: macro.py binds these names at import,
    so patching the config module would leave the value the code actually reads
    untouched. `tests/test_macro_signal.py` and `tests/test_macro_concurrency.py`
    deliberately re-enable it and install a fake transport; their own fixtures
    and monkeypatches run after this one, so they still win -- what this stops
    is the tests that never thought about the provider at all.

    The transport guard makes the failure loud. Without it, a path that called
    out regardless of the flag would reach the internet and the suite would
    stay green, which is precisely how this went unnoticed.
    """
    monkeypatch.setattr(macro, "MACRO_ENABLED", False, raising=False)
    monkeypatch.setattr(macro, "provider", macro.StubMacroProvider(), raising=False)
    monkeypatch.setattr(macro.httpx, "get", _no_network)
    yield
