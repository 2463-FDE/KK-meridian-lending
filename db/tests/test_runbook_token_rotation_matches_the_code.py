"""The rotation runbook claims a fact about the code. Hold it to that.

`docs/runbook.md` tells an operator that rotating `INTERNAL_SERVICE_TOKEN`
requires an outage window, because each service compares against exactly one
configured value with no accept-old-and-new period. That instruction is only safe
while it is true: if a service later accepts a list of valid tokens, the runbook
would still be telling people to take an outage they no longer need -- and worse,
the "zero-downtime rotation is not implemented" paragraph would be false while
reading as authoritative.

So this asserts the claim rather than the wording: every service that checks the
token compares a single configured string, and none of them holds a collection of
accepted values.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
RUNBOOK = REPO / "docs" / "runbook.md"
SERVICES = REPO / "services"


def _configs():
    return sorted(p for p in SERVICES.glob("*/app/config.py")
                  if "INTERNAL_SERVICE_TOKEN" in p.read_text(encoding="utf-8"))


def test_the_runbook_still_documents_rotation():
    text = RUNBOOK.read_text(encoding="utf-8")
    assert "Rotating `INTERNAL_SERVICE_TOKEN`" in text, (
        "the rotation section is gone from the runbook, so an operator rotating "
        "the shared secret has nothing telling them it needs a coordinated restart"
    )
    assert "token_urlsafe(32)" in text, "the runbook no longer says how to generate one"


@pytest.mark.parametrize("config", _configs(), ids=lambda p: p.parent.parent.name)
def test_no_service_accepts_a_list_of_tokens(config):
    """If this fails, the fix is to update the runbook, not to delete the test.

    A service accepting several tokens is a real improvement -- it is what makes a
    zero-downtime rotation possible. It just makes the runbook wrong, and the
    runbook is what someone follows at 2am.
    """
    src = config.read_text(encoding="utf-8")
    m = re.search(r"^INTERNAL_SERVICE_TOKEN\s*=\s*(.+)$", src, re.M)
    assert m, f"{config} names INTERNAL_SERVICE_TOKEN but never assigns it"
    assignment = m.group(1)
    for plural in (".split(", "[", "set(", "tuple("):
        assert plural not in assignment, (
            f"{config.parent.parent.name} appears to accept MULTIPLE tokens "
            f"({assignment.strip()}). That enables zero-downtime rotation, which "
            f"docs/runbook.md currently says is not implemented -- update the "
            f"runbook's rotation section, then this test."
        )


def test_the_check_found_services_to_check():
    """Guards the guard: no configs matched would pass the parametrized test by
    having nothing to run."""
    found = _configs()
    assert len(found) >= 3, (
        f"only {len(found)} service config(s) reference INTERNAL_SERVICE_TOKEN -- "
        f"the rotation claim is being asserted against almost nothing"
    )
