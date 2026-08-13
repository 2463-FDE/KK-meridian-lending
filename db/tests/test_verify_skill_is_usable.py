"""The verification skill must stay usable, and must sweep the surfaces that bit.

A skill file is documentation, so it rots the way documentation rots — and this
one is specifically about that failure mode, which makes an unenforced version of
it embarrassing rather than merely stale.

What is asserted is the skill's *structure*, not its prose: that it has a frontmatter
contract Claude Code can load, that inventory comes before evaluation, and that the
inventory names the surfaces where this repository's real defects were found. A
sweep that omits UI text or LLM prompts would have missed two of them.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SKILL = REPO / ".claude" / "skills" / "verify-regulated-change" / "SKILL.md"


def _text():
    return SKILL.read_text(encoding="utf-8")


def test_the_skill_file_exists_where_claude_code_looks():
    assert SKILL.is_file(), f"no skill at {SKILL.relative_to(REPO)}"
    assert SKILL.parent.name == SKILL.parent.name.lower(), (
        "skill directory names are the invocation name; keep it lowercase-kebab"
    )


def test_it_has_loadable_frontmatter_with_name_and_description():
    """Without both fields the skill does not appear in the available-skills list,
    so it exists and is never offered -- the quietest possible failure."""
    text = _text()
    assert text.startswith("---\n"), "no YAML frontmatter"
    end = text.index("\n---", 3)
    front = text[4:end]

    name = re.search(r"^name:\s*(\S+)", front, re.M)
    desc = re.search(r"^description:\s*(.+)", front, re.M)
    assert name, "frontmatter has no name"
    assert desc, "frontmatter has no description"
    assert name.group(1) == SKILL.parent.name, (
        f"frontmatter name {name.group(1)!r} does not match the directory "
        f"{SKILL.parent.name!r}, which is what gets invoked"
    )
    assert len(desc.group(1)) > 60, (
        "the description is what decides whether this skill is selected for a "
        "task; one line of detail is the difference between used and ignored"
    )


def test_inventory_comes_before_evaluation():
    """The ordering is the point.

    The four questions are useless applied to a claim nobody knew was there, and
    every defect this skill was built from was found in a surface that was not
    part of "the change".
    """
    text = _text()
    inventory = text.index("Step 0 — inventory")
    questions = text.index("## The four questions")
    assert inventory < questions, (
        "the four questions come before the inventory step, so the skill asks "
        "how to verify a claim before establishing which claims exist"
    )


@pytest.mark.parametrize("surface, why", [
    ("docstring", "FastAPI publishes route docstrings on /docs"),
    ("UI text", "a reason-dropdown option is a published rule"),
    ("prompt", "an LLM prompt generates the claim on every call"),
    ("README", "read during onboarding, when nobody re-derives"),
    ("runbook", "read during an incident, when nobody re-derives"),
    ("DEBT.md", "a fix that leaves a D-number stale moved the defect"),
    ("Schema", "the request/response contract"),
])
def test_the_inventory_names_the_surfaces_that_actually_bit(surface, why):
    """Each of these is where a real defect in this repository was found.

    A sweep that omits one of them is the sweep that missed it the first time.
    """
    text = _text()
    assert surface.lower() in text.lower(), (
        f"the claim inventory does not mention {surface!r} -- {why}"
    )


def test_the_four_questions_are_all_present():
    text = _text()
    for q in ("CLAIM", "SOURCE", "TEST", "LIMITATION"):
        assert q in text, f"the {q} step is missing"


def test_it_requires_a_mutation_and_says_what_a_failed_mutation_means():
    """A test that passes with the fix reverted is measuring something else, and
    the skill has to say what to do when that happens."""
    text = _text()
    assert "revert the fix" in text or "negate the guard" in text
    assert "Never commit the mutation" in text
    assert "say so and stop" in text, (
        "the skill does not say what to do when the mutation fails to fail, which "
        "is the moment it would otherwise report an unproven claim as verified"
    )


def test_decision_required_is_a_first_class_verdict():
    """Otherwise the tool picks silently between two defensible answers with
    different compliance consequences."""
    assert "Decision required" in _text()


def test_the_repo_specific_trap_is_recorded():
    """`REVOKE` not sticking is the assumption this codebase got wrong twice; a
    generic checklist would not carry it."""
    text = _text()
    assert "REVOKE" in text
    assert "schema-owning role" in text or "schema owner" in text


# --- the diff command must resolve the PR's own base -------------------------


def test_the_skill_does_not_tell_you_to_diff_against_bare_main():
    """`main...HEAD` is wrong twice over.

    A PR based on a release branch compares against the wrong tree entirely; and
    a local `main` that has not been fetched is stale, which INFLATES the claim
    inventory with other people's merged work and buries the change under review.
    Both failures produce an inventory that looks thorough.
    """
    text = _text()
    bad = [l for l in text.splitlines()
           if "git diff" in l and "main...HEAD" in l and not l.lstrip().startswith("#")]
    assert not bad, f"the skill still recommends a bare main diff: {bad}"


def test_it_resolves_the_pr_base_and_the_merge_base():
    text = _text()
    assert "baseRefName" in text, (
        "the skill does not resolve the PR's own base, so it assumes every PR "
        "targets main"
    )
    assert "git merge-base" in text, (
        "the skill diffs against a branch tip rather than the merge base, so "
        "commits merged into the base after branching appear as this change"
    )
    assert "git fetch origin" in text, (
        "the skill does not fetch the base, so a stale local ref decides the "
        "size of the inventory"
    )


def test_the_origin_main_fallback_is_documented_as_a_fallback():
    """A fallback presented as the default is the original defect with an
    explanation attached."""
    text = _text()
    assert "fallback" in text.lower(), "no fallback is described"
    lower = text.lower()
    i = lower.index("fallback")
    window = lower[max(0, i - 400):i + 400]
    assert "gh" in window or "unavailable" in window, (
        "the fallback does not say WHEN it applies, so a reader will treat it as "
        "the normal path"
    )
