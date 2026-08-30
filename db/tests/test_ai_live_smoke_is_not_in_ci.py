"""The live AI smoke must stay OUT of ordinary CI, and stay reachable by hand.

`scripts/check_ai_live.sh` calls the real model provider. That is the point of
it: the browser suite stubs the model at the network boundary, so a green CI run
says nothing about whether the provider can be reached at demo time -- a gap that
went unnoticed until a TLS-inspecting proxy rotated its root, both live AI
features started returning 502, and the whole suite stayed green because it never
called the provider.

Wiring that script into CI would look like an improvement and would be a
regression in two directions at once:

  * CI has no provider credentials, so every run would fail for want of a
    credential rather than for a defect -- and a suite that fails for reasons
    outside the code is a suite people stop reading;
  * it spends paid quota on every push.

So the invariant is a shape rather than a preference: the script exists, it is
executable by hand, `make ai-live-smoke` reaches it, and no workflow calls it.
All four are asserted, because three of them passing while the fourth quietly
broke is the failure this file exists to prevent.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "check_ai_live.sh"
MAKEFILE = REPO / "Makefile"
WORKFLOWS = REPO / ".github" / "workflows"


def test_the_live_smoke_script_exists():
    """Guard the guard: the assertions below are vacuous without it."""
    assert SCRIPT.is_file(), (
        f"{SCRIPT.relative_to(REPO).as_posix()} is missing. If the live smoke "
        "was deliberately removed, delete this test with it -- a guard that "
        "cannot find its subject passes for the wrong reason."
    )


def test_no_workflow_runs_the_live_smoke():
    """The invariant. CI must never call the real provider."""
    offenders = []
    # Both extensions. Review finding AI-LIVE-MIN-001: globbing `*.yml` alone
    # would let a future `*.yaml` workflow wire this in unnoticed -- the guard
    # would pass while the invariant it exists for had been broken.
    workflows = sorted(
        list(WORKFLOWS.glob("*.yml")) + list(WORKFLOWS.glob("*.yaml"))
    )
    for workflow in workflows:
        body = workflow.read_text(encoding="utf-8")
        if "check_ai_live" in body or "ai-live-smoke" in body:
            offenders.append(workflow.relative_to(REPO).as_posix())

    assert not offenders, (
        "these workflows invoke the live AI smoke: "
        + ", ".join(offenders)
        + ". It calls the real model provider, which CI has no credentials for "
        "and which costs paid quota per run. It is meant to be run by a person "
        "before a demo."
    )


def test_make_reaches_the_live_smoke():
    """And it has to be easy to run, or it will not be run.

    A check that exists but that nobody can remember how to invoke is a check
    that does not happen before the demo it exists to protect.
    """
    body = MAKEFILE.read_text(encoding="utf-8")

    assert re.search(r"^ai-live-smoke:", body, re.M), (
        "the Makefile no longer has an `ai-live-smoke` target"
    )
    assert "check_ai_live.sh" in body, (
        "the `ai-live-smoke` target no longer runs scripts/check_ai_live.sh"
    )
    assert re.search(r"^\.PHONY:.*\bai-live-smoke\b", body, re.M), (
        "`ai-live-smoke` is missing from .PHONY, so a file of that name would "
        "silently shadow the target"
    )


def test_the_smoke_reports_a_three_way_exit_contract():
    """0 ready / 1 not ready / 2 could not run.

    Collapsing 1 and 2 is the tempting simplification and the wrong one: "the AI
    path is broken" and "I could not tell" call for different responses in the
    hour before a demo. The other checks in `scripts/` use the same contract.
    """
    body = SCRIPT.read_text(encoding="utf-8")

    assert "exit 2" in body, "no 'could not run' exit path"
    assert "exit 1" in body, "no 'not ready' exit path"
    assert "exit 0" in body, "no success exit path"


def test_the_smoke_does_not_print_what_it_is_checking():
    """Privacy, asserted rather than trusted.

    A readiness check that echoed a prompt, an answer, a summary or a credential
    would be its own incident. The script prints categorical status only, so
    these names must not appear in anything it echoes.
    """
    body = SCRIPT.read_text(encoding="utf-8")

    # Every line the script prints.
    printed = "\n".join(
        line for line in body.splitlines()
        if re.match(r"\s*(echo|ok|bad|step|cannot)\b", line)
    )

    # Interpolation and credentials, NOT English words. The first version of
    # this guard forbade the substring "answer", which flagged the status line
    # "policy chat answered but without grounding evidence" -- prose describing
    # a result is not a leak of it, and a guard that cannot tell the difference
    # gets weakened or deleted rather than obeyed.
    for leak in (
        "$UW", "$AD",          # session tokens
        "_ai1", "_ai2", "_ai3",  # files holding raw provider responses
        "$(cat",                # any file contents at all
        "AWS_", "BEDROCK", "ANTHROPIC_", "API_KEY", "api_key", "SECRET",
    ):
        assert leak not in printed, (
            f"the live smoke prints {leak!r}, which risks echoing a credential, "
            "a prompt or a model response"
        )

    # And the response bodies must never be dumped, however they are reached.
    assert not re.search(r"(cat|head|tail)\s+/tmp/_ai", body), (
        "the live smoke dumps a raw provider response file"
    )
