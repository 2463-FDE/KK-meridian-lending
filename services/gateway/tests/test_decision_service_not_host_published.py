"""Regression test for the decision-service host-port bypass.

Review finding: docker-compose.yml published decision-service on host port 8004
while POST /decisions had no auth of its own (see decision-service's
routers/decisions.py X-Internal-Token check, added alongside this fix).
Anyone on the host could hit http://localhost:8004/decisions directly,
bypassing the gateway's staff-only /decision/* check entirely, submit SSNs,
and overwrite the decision row for any application. This asserts
decision-service is never re-published to the host.
"""
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


def test_decision_service_has_no_host_port_mapping():
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    block = _service_block(text, "decision-service")

    assert "ports:" not in block, (
        "decision-service must not publish a host port -- it handles SSNs and "
        "trusts application_id alone from callers; anyone on the host could "
        "bypass the gateway's staff-only /decision/* check entirely"
    )
