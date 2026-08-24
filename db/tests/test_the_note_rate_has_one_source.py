"""The note rate cannot be two numbers.

The rate had five copies: two frontend constants, origination's `OfferIn`
default, disclosure-service's `OfferIn` default, and -- the one that actually
decided the borrower's rate -- `disclosure-service/app/fees.py::NOTE_RATE_PCT`.
PR #80 moved pricing to a configured value and review found the sixth problem in
that list: origination could publish 8.50, refuse any caller who sent something
else, and disclosure would still build and store the loan at 7.99. The rate the
borrower got was decided by a constant nobody configured.

Two services, two images, no shared library (ADR 0002), so the figure is read
twice -- from the same environment variable. This is what stops those two reads
from being able to disagree, and it is the same shape as
`test_maker_checker_limits_have_one_source.py`: several copies of one number, and
a test whose job is to fail the moment they diverge.
"""
import pathlib
import re

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
COMPOSE = REPO / "docker-compose.yml"

#: The variable both services read, and the services that must read it.
ENV_VAR = "DEMO_NOTE_RATE_PCT"
CONFIGS = {
    "origination-service": REPO / "services" / "origination-service" / "app" / "config.py",
    "disclosure-service": REPO / "services" / "disclosure-service" / "app" / "config.py",
}


def _default_in(path: pathlib.Path) -> str:
    """The literal default beside the getenv call, e.g. "7.99"."""
    text = path.read_text(encoding="utf-8")
    match = re.search(
        rf"{ENV_VAR}\s*=\s*float\(os\.getenv\(\s*[\"']{ENV_VAR}[\"']\s*,\s*[\"']([\d.]+)[\"']",
        text)
    assert match, f"{path.relative_to(REPO)} does not read {ENV_VAR} with a literal default"
    return match.group(1)


def test_both_services_read_the_same_environment_variable():
    for name, path in CONFIGS.items():
        text = path.read_text(encoding="utf-8")
        assert ENV_VAR in text, (
            f"{name} does not read {ENV_VAR}, so it is pricing offers from "
            f"something else")


def test_the_two_defaults_are_the_same_number():
    """A default is what runs when nothing is set, which is every developer
    machine that has not exported the variable. Two different defaults would be
    the same split with a smaller blast radius."""
    defaults = {name: _default_in(path) for name, path in CONFIGS.items()}

    assert len(set(defaults.values())) == 1, (
        f"the services disagree about the default note rate: {defaults}. One of "
        f"them would price offers the other refuses to publish")


def test_compose_declares_the_variable_for_every_service_that_reads_it():
    """Compose passes a host variable only to services that name it, so a
    service reading it without declaring it gets the default -- silently, and only
    on the machines where the other one did not."""
    compose = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))

    missing = []
    for name in CONFIGS:
        env = compose["services"][name].get("environment") or {}
        keys = set(env) if isinstance(env, dict) else {
            item.split("=", 1)[0] for item in env}
        if ENV_VAR not in keys:
            missing.append(name)

    assert not missing, (
        f"{missing} read {ENV_VAR} without declaring it in docker-compose.yml")


def test_no_module_holds_a_live_note_rate_constant():
    """The removed one, and any replacement for it.

    `fees.NOTE_RATE_PCT` was a module constant that quietly decided the
    contractual rate. What makes this checkable is that a note-rate constant has
    a recognisable shape: a name mentioning the rate, assigned a number.
    """
    offenders = []
    pattern = re.compile(
        r"^\s*(?!#)([A-Z_]*NOTE_RATE[A-Z_]*)\s*=\s*(?:Decimal\(\s*[\"']?)?([\d.]+)",
        re.M)

    for path in sorted((REPO / "services").rglob("*.py")):
        parts = set(path.parts)
        if "tests" in parts or "__pycache__" in parts:
            continue
        for match in pattern.finditer(path.read_text(encoding="utf-8", errors="replace")):
            name = match.group(1)
            # The configured reads are the authority, not a hardcoded constant:
            # they are `float(os.getenv(...))`, which this pattern does not match.
            offenders.append(f"{path.relative_to(REPO)}: {name} = {match.group(2)}")

    assert not offenders, (
        "a module-level note-rate constant exists. The rate is configuration "
        "read from " + ENV_VAR + ", and a constant beside it is the copy that "
        "ends up deciding what the borrower pays:\n" + "\n".join(offenders))


def test_the_publishing_service_and_the_calculating_service_agree_at_runtime():
    """Read both modules the way the services do, and compare.

    A defaults comparison catches a typo; this catches a divergence introduced by
    reading a different variable, applying a different transformation, or
    rounding one side.
    """
    values = {}
    for name, path in CONFIGS.items():
        namespace: dict = {}
        # Executed with a stub `os` so the module's own getenv default is what is
        # measured, without importing service packages into this test job.
        source = path.read_text(encoding="utf-8")
        block = "\n".join(
            line for line in source.splitlines()
            if line.startswith(f"{ENV_VAR}") or line.startswith("import os"))
        exec(compile(block, str(path), "exec"), namespace)  # noqa: S102
        values[name] = namespace[ENV_VAR]

    assert len(set(values.values())) == 1, (
        f"the two services compute different note rates from the same "
        f"environment: {values}")
    assert values["origination-service"] > 0
