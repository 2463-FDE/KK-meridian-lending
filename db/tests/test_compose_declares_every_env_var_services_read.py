"""If a variable is REQUIRED for one service, no service that reads it may quietly do without it.

That is the invariant, and it is derived rather than listed. The required set
comes from docker-compose.yml itself -- every `${VAR:?...}` in the file -- and the
readers come from each service's own source. Nothing here names a service or a
variable, so nothing here can go stale.

It exists because this is the fifth time on this branch that a hand-maintained
list of protected things read as complete while missing one entry:

  1. the host-port test listed the services it checked, and omitted two;
  2. the money-route check proved a header was present, not that a guard ran;
  3. the compose ENVIRONMENT declaration covered servicing but not payment;
  4. the payment preflight probed a table the real money path never writes;
  5. this one.

Each fix has been the same fix: derive the expectation from the source instead of
writing it down.

Why declaring matters even though every service has `env_file: .env`: that entry
is `required: false`, so on a clean checkout -- CI, or anyone who has not run the
bootstrap -- the file is simply absent. Compose also passes a HOST variable
through only to services that name it. So a service reading a variable it never
declares works on the machine of whoever wrote it and nowhere else. That is
exactly how payment-service came to start with ENVIRONMENT unset: locally a
developer's own .env supplied it, and CI had no .env to supply anything, so
ALLOW_PAYMENT_STUB was false and the deterministic test processor refused every
authorization.
"""
import pathlib
import re

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
COMPOSE = REPO / "docker-compose.yml"
SERVICES_DIR = REPO / "services"


def _compose_text() -> str:
    return COMPOSE.read_text(encoding="utf-8")


def _required_vars() -> set[str]:
    """Variables compose refuses to start without, for at least one service."""
    return set(re.findall(r"\$\{(\w+):\?", _compose_text()))


def _env_vars_read_by(service_path: pathlib.Path) -> set[str]:
    """Every environment variable name the service's own app code reads.

    Tests are excluded: they set their own variables and run outside compose.
    """
    found: set[str] = set()
    for py in service_path.rglob("*.py"):
        if "tests" in py.parts or "__pycache__" in py.parts:
            continue
        src = py.read_text(encoding="utf-8", errors="replace")
        found |= set(re.findall(r"os\.getenv\(\s*[\"'](\w+)[\"']", src))
        found |= set(re.findall(r"os\.environ\[\s*[\"'](\w+)[\"']\s*\]", src))
        found |= set(re.findall(r"os\.environ\.get\(\s*[\"'](\w+)[\"']", src))
    return found


def _declared_for(name: str) -> set[str]:
    compose = yaml.safe_load(_compose_text())
    svc = compose.get("services", {}).get(name) or {}
    env = svc.get("environment") or {}
    if isinstance(env, list):                     # `- KEY=value` form
        return {item.split("=", 1)[0] for item in env}
    return set(env.keys())


def _service_dirs():
    return sorted(p for p in SERVICES_DIR.iterdir()
                  if p.is_dir() and (p / "app").exists())


@pytest.mark.parametrize("service_path", _service_dirs(), ids=lambda p: p.name)
def test_a_service_that_reads_a_required_var_also_declares_it(service_path):
    compose = yaml.safe_load(_compose_text())
    name = service_path.name
    if name not in compose.get("services", {}):
        pytest.skip(f"{name} is not a compose service")

    should_declare = _env_vars_read_by(service_path) & _required_vars()
    missing = sorted(should_declare - _declared_for(name))

    assert not missing, (
        f"{name} reads {missing}, which docker-compose.yml declares REQUIRED for "
        f"at least one other service, but never declares for this one. Compose "
        f"passes a host variable through only to services that name it, and "
        f"`env_file: .env` is required:false -- so on a clean checkout this "
        f"service starts with {'it' if len(missing) == 1 else 'them'} unset while "
        f"the rest of the stack refuses to start without the same value."
    )


def test_the_variables_that_gate_real_money_are_required_not_defaulted():
    """A `:-default` on these would put the fail-closed behaviour back to sleep.

    ENVIRONMENT decides whether a stub processor may authorize a charge and
    whether a weak internal token is tolerated; INTERNAL_SERVICE_TOKEN is the
    application-level defence on the routes that move money. A default for either
    means an operator who configures nothing still gets a system that looks fine.
    """
    raw = _compose_text()
    for var in ("ENVIRONMENT", "INTERNAL_SERVICE_TOKEN"):
        defaulted = re.findall(rf"\$\{{{var}:-[^}}]*\}}", raw)
        assert not defaulted, (
            f"{var} has a default in the base compose file ({defaulted}), so a "
            f"deployment that sets nothing still starts"
        )
        assert var in _required_vars(), (
            f"{var} is never declared as required (`${{{var}:?...}}`) in the base "
            f"compose file"
        )


def test_the_derivation_actually_finds_something():
    """Guards the guard.

    Both halves of the check above are derived, so both can silently degrade to
    the empty set -- a refactor to a settings object stops the reader matching, or
    someone drops the last `:?` and the required set empties. Either way every
    assertion above would pass by having nothing to compare, and would keep
    passing while the invariant rotted.
    """
    assert {"ENVIRONMENT", "INTERNAL_SERVICE_TOKEN"} <= _required_vars(), (
        "the required-variable reader found neither of the two variables this "
        "check exists for, so the tests above are comparing empty sets"
    )
    assert "ENVIRONMENT" in _env_vars_read_by(SERVICES_DIR / "payment-service")
    assert "INTERNAL_SERVICE_TOKEN" in _env_vars_read_by(SERVICES_DIR / "servicing-service")
