"""Regression test for the decision/disclosure/payment-service host-port bypass.

Review finding: docker-compose.yml published decision-service, disclosure-
service, and payment-service on host ports 8004-8006 while their POST routes
had no auth of their own (see each service's routers/*.py X-Internal-Token
check, added alongside this fix). Anyone on the host could hit any of them
directly, bypassing the gateway's staff-only/ownership checks entirely --
submit SSNs and overwrite a decision, overwrite a real loan's TILA numbers, or
charge a card. This asserts none of the three are ever re-published to the
host.
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


@pytest.mark.parametrize("service_name", ["decision-service", "disclosure-service", "payment-service"])
def test_internal_service_has_no_host_port_mapping(service_name):
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    block = _service_block(text, service_name)

    assert "ports:" not in block, (
        f"{service_name} must not publish a host port -- it trusts the gateway "
        "to have already authenticated/authorized the caller and has no "
        "meaningful auth of its own beyond the X-Internal-Token defense-in-"
        "depth check; anyone on the host could bypass the gateway entirely"
    )
