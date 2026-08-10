"""The guards in conftest.py, asserted somewhere pytest actually collects.

conftest.py sets `MACRO_ENABLED=0` before `app.config` is imported and installs
a transport that raises if anything calls out anyway. An assertion written as a
`test_` function inside conftest.py looked like it guarded that, and did not:
pytest loads conftest for fixtures and plugins but does not collect tests from
it, so the check never ran and an import reordering would have gone unreported.
Reviewed on PR #13 -- a guard that cannot fail is decoration.

These live in a collected module and do run.
"""
import httpx
import pytest

from app import config, macro


def test_the_module_constant_was_disabled_before_import():
    """Layer 1: the environment variable beat the import.

    `app/config.py` reads MACRO_ENABLED once, at module scope. If conftest.py
    ever imports config before setting it -- an import block reordered by a
    formatter would do it -- this is True and every test in the service is one
    unmocked call away from BLS.
    """
    assert config.MACRO_ENABLED is False


def test_the_provider_in_use_is_the_stub():
    """The module-level provider is built from that constant at import time."""
    assert isinstance(macro.provider, macro.StubMacroProvider)


def test_the_autouse_guard_blocks_a_real_outbound_call():
    """Layer 2: an unstubbed call fails loudly instead of reaching the internet.

    Asserted by calling it. The failure mode being guarded against is a test
    that silently succeeds against the live API, which is indistinguishable
    from a passing test until someone looks at a quota.
    """
    with pytest.raises(AssertionError, match="real outbound macro request"):
        httpx.get("https://api.bls.gov/publicAPI/v1/timeseries/data/LNS14000000")
