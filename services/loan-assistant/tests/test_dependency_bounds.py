"""`anthropic` must keep an upper bound until someone moves it deliberately.

On 2026-08-20 the same commit passed CI at 19:54 and failed at 20:10. Nothing in
the tree changed between the two runs; `anthropic` released 1.0.0 and
`requirements.txt` said `>=0.40.0`, so CI resolved a new major at install time.
The visible break was `AnthropicBedrock()` raising when no AWS region can be
inferred rather than tolerating it -- but a major bump is not a promise that
this was the only behaviour change, and the rest have not been read.

This test is one line of intent that a `pip install -U` cannot quietly undo. It
does not check WHICH bound is set, only that one exists: raising the ceiling
after reading the changelog against `app/llm_client.py` is the correct move and
this must not stand in its way. Removing the ceiling entirely is the thing that
costs an afternoon, because the failure lands on whichever unrelated pull
request happens to run next.
"""
import pathlib
import re

REQUIREMENTS = pathlib.Path(__file__).resolve().parents[1] / "requirements.txt"

#: Packages that have already broken this service's CI through an unbounded
#: floor. Membership is earned by an incident, not by policy -- a blanket
#: "pin everything" rule would freeze dependencies nobody has had trouble with
#: and turn every routine upgrade into a diff.
MUST_HAVE_AN_UPPER_BOUND = ("anthropic",)


def _requirement(name: str) -> str:
    for line in REQUIREMENTS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith("#") or not line:
            continue
        if re.match(rf"^{re.escape(name)}\b", line):
            return line
    raise AssertionError(f"{name} is not in {REQUIREMENTS.name}")


def test_the_requirement_is_present_to_check():
    """Guard the guard: a renamed or removed package would pass vacuously."""
    for name in MUST_HAVE_AN_UPPER_BOUND:
        assert _requirement(name)


def test_a_package_that_has_broken_ci_keeps_its_ceiling():
    for name in MUST_HAVE_AN_UPPER_BOUND:
        line = _requirement(name)
        assert "<" in line or "==" in line or "~=" in line, (
            f"{name} has no upper bound ({line!r}). It broke this service's CI "
            f"once by resolving a new major at install time, which fails on "
            f"whichever unrelated pull request runs next rather than on the "
            f"change that caused it. Raise the bound deliberately after reading "
            f"the changelog against app/llm_client.py -- do not remove it."
        )
