"""Regression test for the internal-service host-port bypass.

Review finding: docker-compose.yml published decision-service, disclosure-
service, and payment-service on host ports 8004-8006 while their POST routes
had no auth of their own (see each service's routers/*.py X-Internal-Token
check, added alongside this fix). Anyone on the host could hit any of them
directly, bypassing the gateway's staff-only/ownership checks entirely --
submit SSNs and overwrite a decision, overwrite a real loan's TILA numbers, or
charge a card. origination-service had the same gap (host port 8001, staff
routes trusting X-User-Role alone) and was closed the same way. This asserts
none of them are ever re-published to the host.

kyc-service was added to this list two months later, and the delay is the point
worth recording. It had the same defect as the original four -- host port 8003,
no X-Internal-Token, an unauthenticated POST /kyc/check writing a kyc_checks row
for any applicant_id -- but it was omitted from both the PR #6 fix and this
parametrize list. ARCHITECTURE.md named the gap in prose and assigned it to
PR #8; PR #8 merged without it, and prose does not fail a build. A partial
enumeration in a security regression test reads exactly like a complete one, so
the omission looked like coverage for as long as nobody re-derived the list from
docker-compose.yml. Any future backend service belongs here on the day it is
added, not on the day someone notices.
"""
import pytest
from pathlib import Path

COMPOSE_PATH = Path(__file__).resolve().parents[3] / "docker-compose.yml"


def _service_block(compose_text: str, service_name: str) -> str:
    """Lines from `service_name:` up to (not including) the next line at the
    same or shallower indentation -- i.e. the next sibling service key."""
    lines = compose_text.splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == f"{service_name}:")
    indent = len(lines[start]) - len(lines[start].lstrip(" "))
    end = len(lines)
    for i in range(start + 1, len(lines)):
        line = lines[i]
        if line.strip() and (len(line) - len(line.lstrip(" "))) <= indent:
            end = i
            break
    return "\n".join(lines[start:end])


@pytest.mark.parametrize(
    "service_name",
    [
        "decision-service",
        "disclosure-service",
        "payment-service",
        "origination-service",
        "servicing-service",
        "kyc-service",
        "loan-assistant",
    ],
)
def test_internal_service_has_no_host_port_mapping(service_name):
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    block = _service_block(text, service_name)

    assert "ports:" not in block, (
        f"{service_name} must not publish a host port -- it trusts the gateway "
        "to have already authenticated/authorized the caller and has no "
        "meaningful auth of its own beyond the X-Internal-Token defense-in-"
        "depth check; anyone on the host could bypass the gateway entirely"
    )


def test_the_list_above_covers_every_backend_service():
    """The hand-written list is the thing that failed, so derive it too.

    kyc-service was missing from the parametrize list above for two months while
    publishing 8003. The list looked authoritative and was not. This test builds
    the set from `services/` on disk instead, so a new backend service is covered
    the moment its directory exists -- and if someone adds one that legitimately
    needs a host port, they have to say so here rather than silently widening the
    boundary.
    """
    services_dir = COMPOSE_PATH.parent / "services"
    on_disk = {
        p.name for p in services_dir.iterdir()
        # A service is a directory with a Dockerfile; this skips tooling debris
        # like .pytest_cache that would otherwise fail the test for no reason.
        if p.is_dir() and (p / "Dockerfile").is_file()
    }

    # gateway is the deliberate exception: it is the authenticating front door and
    # publishing 8000 is the whole point. loan-assistant is reached only through
    # the gateway and is covered by the same rule as the rest.
    expected = on_disk - {"gateway"}

    covered = set(
        test_internal_service_has_no_host_port_mapping.pytestmark[0].args[1]
    )
    missing = expected - covered
    assert not missing, (
        f"backend services not covered by the host-port assertion: {sorted(missing)}. "
        "Add them to the parametrize list above, or -- if one genuinely needs a "
        "host port -- add it to the exception set here with the reason, so the "
        "decision is recorded rather than implied by an omission."
    )
