"""The maker-checker limits are written down in four places. They must agree.

The 2026-08-19 demo feedback asked for one thing here: that the configuration
note point at ONE source. Today the approved figures appear in four files --
`adr/0011-maker-checker-for-servicing-adjustments.md`,
`specs/0002-maker-checker-self-approval.md`, `.env.example` and
`scripts/bootstrap_env.py` -- and nothing checks that they still say the same
number. Four copies of a money limit is three chances to drift, and the drift is
invisible: every one of them looks authoritative on its own page.

ADR 0011 is the source. It is where the approval is recorded -- who approved the
values, on what date, and the standing caveat that they are cohort/demo
configuration rather than Lending Operations policy. The other three are
deployment copies, and this test asserts they are copies rather than opinions.

**No threshold is invented here, and this test cannot invent one.** It reads the
approved figures out of the ADR and compares; if Lending Operations later sets
different values, the ADR changes and the copies must follow it. What the test
forbids is a copy moving on its own.

It also asserts the caveat itself survives. A limit that quietly loses the words
"not Lending Operations policy" reads, six months later, as an approved policy
nobody can trace -- which is the same defect as a stale citation, arriving
through an omission rather than an edit.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

ADR = REPO / "adr" / "0011-maker-checker-for-servicing-adjustments.md"
SPEC = REPO / "specs" / "0002-maker-checker-self-approval.md"
ENV_EXAMPLE = REPO / ".env.example"
BOOTSTRAP = REPO / "scripts" / "bootstrap_env.py"
COMPOSE = REPO / "docker-compose.yml"
CONFIG = REPO / "services" / "servicing-service" / "app" / "config.py"

MONEY_LIMITS = ("MAKER_CHECKER_ADMIN_THRESHOLD", "MAKER_CHECKER_MAX_DELTA")


def _text(path: pathlib.Path) -> str:
    return path.read_text(encoding="utf-8")


def _approved_money_limits() -> dict:
    """The figures as ADR 0011's own table states them.

    Read from the table rather than hardcoded in this file, because a constant
    here would become a fifth copy -- and the one nobody would think to look at.
    """
    found = {}
    for line in _text(ADR).splitlines():
        if not line.startswith("|"):
            continue
        for name in MONEY_LIMITS:
            if f"`{name}`" in line:
                match = re.search(r"\|\s*\**(\d+\.\d{2})\**\s*—", line)
                assert match, (
                    f"ADR 0011's row for {name} no longer states a figure in the "
                    f"form '500.00 — ...'; this test reads the source and cannot "
                    f"guess it: {line}"
                )
                found[name] = match.group(1)
    return found


def _approved_statuses() -> str:
    """The permitted loan statuses, as the ADR states them."""
    for line in _text(ADR).splitlines():
        if line.startswith("|") and "Permitted loan statuses" in line:
            match = re.search(r'`\{"(\w+)"\}`', line)
            assert match, f"ADR 0011 no longer states the permitted statuses: {line}"
            return match.group(1)
    pytest.fail("ADR 0011 has no permitted-loan-statuses row")


def _env_value(text: str, key: str) -> str:
    match = re.search(rf"^{key}=(.+)$", text, re.MULTILINE)
    assert match, f"{key} is not set in the file under test"
    return match.group(1).strip()


def test_the_adr_states_a_figure_for_each_money_limit():
    """Guard the guard: every assertion below is read out of this table."""
    limits = _approved_money_limits()

    assert set(limits) == set(MONEY_LIMITS), (
        f"ADR 0011 states figures for {sorted(limits)}; expected both limits")


def test_the_adr_records_who_approved_the_values_and_when():
    """A number with no approver is a number somebody chose.

    The whole reason these live in configuration rather than in a CHECK
    constraint is that a human approved them for one environment. If that
    sentence goes, the figures become anonymous and the caveat below is
    unattributable.
    """
    text = _text(ADR)

    assert "2026-08-16" in text, "ADR 0011 no longer records the approval date"
    assert "project owner approved" in text, (
        "ADR 0011 no longer records who approved the configured limits")


@pytest.mark.parametrize("path", [ADR, SPEC])
def test_the_caveat_that_these_are_not_policy_survives(path):
    """Demo configuration must not quietly become approved policy.

    Asserted on both documents a reader might land on first.
    """
    text = _text(path).lower()

    assert "not lending operations policy" in text or "not** lending operations policy" in text, (
        f"{path.name} no longer says the configured limits are not Lending "
        f"Operations policy")
    assert "cohort/demo" in text


def test_the_env_example_matches_the_adr():
    """The deployment copy a developer actually reads."""
    text = _text(ENV_EXAMPLE)
    limits = _approved_money_limits()

    for name, approved in limits.items():
        assert _env_value(text, name) == approved, (
            f".env.example sets {name} to a value ADR 0011 did not approve")
    assert _env_value(text, "MAKER_CHECKER_PERMITTED_LOAN_STATUSES") == _approved_statuses()


def test_the_env_example_points_at_the_source():
    """A copy that does not name its source is indistinguishable from an origin.

    The completion proof the demo feedback asked for, stated as an assertion:
    the configuration note points at ONE place, and that place is where the
    approval lives.
    """
    text = _text(ENV_EXAMPLE)
    block = text[text.index("MAKER_CHECKER_ADMIN_THRESHOLD") - 1200:
                 text.index("MAKER_CHECKER_ADMIN_THRESHOLD")]

    assert "adr/0011" in block.lower(), (
        ".env.example's maker-checker block does not cite ADR 0011, so a reader "
        "cannot tell which of the four copies of these figures is the source")


def test_the_bootstrap_script_matches_the_adr():
    """The copy that writes a developer's `.env` for them.

    The most dangerous of the three: it is executed rather than read, so a drift
    here reaches a running stack without anybody looking at the number.
    """
    text = _text(BOOTSTRAP)
    limits = _approved_money_limits()

    for name, approved in limits.items():
        match = re.search(rf'"{name}",\s*"([^"]+)"', text)
        assert match, f"{BOOTSTRAP.name} no longer seeds {name}"
        assert match.group(1) == approved, (
            f"{BOOTSTRAP.name} seeds {name}={match.group(1)}, ADR 0011 approved "
            f"{approved}")
    match = re.search(r'"MAKER_CHECKER_PERMITTED_LOAN_STATUSES",\s*"([^"]+)"', text)
    assert match and match.group(1) == _approved_statuses()


def test_the_spec_matches_the_adr():
    """Spec 0002 restates the figures in its own approved-values table.

    Kept in step rather than deleted: the spec is the document a reviewer reads
    against the acceptance criteria, and sending them to another file for the
    numbers those criteria are about would be worse. It just may not disagree.
    """
    text = _text(SPEC)
    limits = _approved_money_limits()

    for name, approved in limits.items():
        row = [line for line in text.splitlines()
               if line.lstrip("> ").startswith(f"| `{name}`")]
        assert row, f"spec 0002 no longer states an approved value for {name}"
        assert approved in row[0], (
            f"spec 0002 states a different figure for {name} than ADR 0011 "
            f"approved ({approved}): {row[0]}")


def test_no_limit_has_a_code_default():
    """The property that makes the copies safe to have at all.

    Every copy above is configuration. If the service defaulted a missing limit,
    a stack could run on a figure that appears in no document and was approved by
    nobody -- and the tests that prove the limits work would pass against no
    limit at all.
    """
    text = _text(CONFIG)

    for name in MONEY_LIMITS + ("MAKER_CHECKER_PERMITTED_LOAN_STATUSES",):
        match = re.search(rf'os\.getenv\(\s*"{name}",\s*("")\s*\)', text)
        assert match, (
            f"{name} is not read with an empty default in config.py -- either the "
            f"read moved, or it now defaults to something nobody approved")


def test_compose_refuses_to_start_without_them():
    """The deployment boundary, for the same reason.

    `${VAR:?...}` fails the stack rather than passing an empty string through to
    a service that would then refuse at boot with a less obvious message.
    """
    text = _text(COMPOSE)

    for name in MONEY_LIMITS + ("MAKER_CHECKER_PERMITTED_LOAN_STATUSES",):
        assert f"${{{name}:?" in text, (
            f"docker-compose.yml no longer requires {name}")
