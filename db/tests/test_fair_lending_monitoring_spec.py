"""Guards on spec 0003, and only the claims worth guarding.

A spec is prose, and prose tests rot fast: assert every sentence and the next
honest edit fails the suite for no reason. So this checks the handful of things
that would make the document dangerous rather than merely out of date —
principally that it never acquires a fairness claim it cannot support, and that
it does not drift apart from the model card, which is the other artefact a
regulator would be handed.

The cross-document checks exist because the two files answer the same question
from different angles. `docs/model_card.md` says no fairness testing of the
model has been done; spec 0003 says why that cannot change yet. If one of those
is edited and the other is not, the pair starts contradicting itself, and a
contradiction between two governance artefacts is worse than either being
silent.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SPEC = REPO / "specs" / "0003-fair-lending-monitoring.md"
MODEL_CARD = REPO / "docs" / "model_card.md"


@pytest.fixture(scope="module")
def spec() -> str:
    assert SPEC.is_file(), f"spec 0003 is missing: {SPEC}"
    return SPEC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def card() -> str:
    return MODEL_CARD.read_text(encoding="utf-8")


# --------------------------------------------------------------------------
# It covers both halves the client asked for.
# --------------------------------------------------------------------------

def test_the_spec_covers_denial_reason_accuracy(spec):
    lowered = spec.lower()

    assert "denial-reason accuracy" in lowered
    assert "reason_codes" in spec, "the authoritative field is not named"
    assert "model_version" in spec, (
        "a reason distribution that does not name the model version describes "
        "nothing"
    )


def test_the_spec_covers_the_disparity_check(spec):
    lowered = spec.lower()

    assert "four-fifths" in lowered
    assert "zip3" in lowered
    assert "min_group_size" in spec, "the small-group guard is not specified"


def test_the_spec_requires_distinct_reason_reporting(spec):
    """The brief's first question was how many distinct reasons the model
    emits. A spec that does not require that measurement does not answer it."""
    assert re.search(r"distinct", spec, re.I)
    assert re.search(r"frequency", spec, re.I)


# --------------------------------------------------------------------------
# The claims it must never make.
# --------------------------------------------------------------------------

def test_the_spec_does_not_claim_zip3_proves_model_fairness(spec):
    """ZIP3 measures outcomes. Outcomes are the product of the model, the
    thresholds and every manual review in between, so a ZIP3 result cannot be
    attributed to the model."""
    assert "cannot attribute" in spec.lower() or "cannot support" in spec.lower(), (
        "the spec does not state the limit of what the ZIP3 screen can show")

    # The phrase itself is unavoidable: the spec has to name the claim it is
    # refusing to make, and its own section heading is "What is required before
    # anyone claims *this model is fair*". A blunt "this phrase must not appear"
    # check failed on exactly that heading, which is the sentence doing the
    # refusing. So each occurrence is judged in context instead.
    negating = re.compile(
        r"\b(cannot|can not|must not|does not|do not|no|not|never|without|"
        r"before anyone claims|refus\w*)\b", re.I)

    def _sentence_around(text: str, index: int) -> str:
        """The sentence the match sits in.

        Scoped to the sentence rather than a character window, because a window
        wide enough to catch the negation is also wide enough to catch a
        negation belonging to a neighbouring sentence -- which is how the first
        version of this check passed a deliberately planted fairness claim.
        """
        start = max(text.rfind(". ", 0, index), text.rfind("\n\n", 0, index)) + 1
        end = text.find(". ", index)
        end = len(text) if end == -1 else end + 1
        return text[max(start, 0):end]

    for match in re.finditer(r"model is fair\b(?!ness)", spec, re.I):
        sentence = _sentence_around(spec, match.start())
        assert negating.search(sentence), (
            f"spec 0003 asserts model fairness: {sentence.strip()!r}")

    # Same treatment for the other affirmative forms, and for the same reason:
    # the spec describes this very guard ("does not claim ZIP3 proves model
    # fairness"), so a bare pattern match flags the sentence promising not to
    # do the thing.
    for pattern in (r"proves? (?:the )?model fairness",
                    r"demonstrates? (?:that )?the model is fair",
                    r"the model has been shown to be fair"):
        for match in re.finditer(pattern, spec, re.I):
            sentence = _sentence_around(spec, match.start())
            assert negating.search(sentence), (
                f"spec 0003 asserts model fairness: {sentence.strip()!r}")


def test_the_spec_states_that_a_fairness_claim_cannot_be_made_today(spec):
    assert re.search(r"cannot (today )?make a fairness claim|MUST NOT make one",
                     spec, re.I), (
        "the operative conclusion is missing")


def test_the_spec_identifies_the_data_missing_before_a_fairness_claim(spec):
    lowered = spec.lower()

    assert "protected-class" in lowered
    assert "vendor fairness documentation" in lowered
    assert "sample size" in lowered


def test_the_spec_refuses_to_manufacture_protected_class_data(spec):
    assert re.search(r"MUST NOT be manufactured|must not be manufactured", spec), (
        "nothing forbids synthesising a protected class, which is the most "
        "tempting way to close the gap and the worst")


def test_the_spec_does_not_invent_vendor_reason_codes(spec):
    """Named only inside the sentence that forbids inventing them."""
    for invented in ("HIGH_DTI", "DEROGATORY_HISTORY"):
        if invented in spec:
            context = spec[max(0, spec.index(invented) - 400):spec.index(invented)]
            assert "invent" in context.lower(), (
                f"{invented} appears outside the passage forbidding invention")


def test_the_spec_cites_the_regulation_not_the_withdrawn_circulars(spec):
    """ADR 0006 already made this mistake once and recorded it."""
    assert "12 CFR 1002.9" in spec

    for circular in ("2022-03", "2023-03"):
        if circular in spec:
            assert "withdrawn" in spec.lower(), (
                f"circular {circular} is cited without noting it was withdrawn")


# --------------------------------------------------------------------------
# It must not contradict the other governance artefact.
# --------------------------------------------------------------------------

def test_the_spec_and_the_model_card_agree_that_model_fairness_is_untested(spec, card):
    card_says_untested = re.search(
        r"no fairness|not been run|never been (run|performed)", card, re.I)
    spec_says_untested = re.search(
        r"cannot (today )?make a fairness claim|MUST NOT make one", spec, re.I)

    assert card_says_untested, (
        "the model card no longer says model fairness is untested -- if that "
        "changed because evidence landed, spec 0003 needs updating in the same "
        "change")
    assert spec_says_untested, (
        "spec 0003 no longer says a fairness claim cannot be made, but the "
        "model card still says fairness is untested")


def test_the_spec_does_not_invent_an_approval_authority(spec):
    """The rollout position is an engineering one. Approval authority is not
    defined in this repository and inventing it would be the maker-checker
    threshold mistake again (DEBT D8)."""
    assert "approval authority is not defined" in spec.lower()


def test_every_repository_path_the_spec_cites_resolves(spec):
    """`db/tests/test_docs_citations_resolve.py` covers the tracked docs set;
    this checks the same property for this file directly, so the spec cannot
    ship with a dead pointer while that suite is scoped elsewhere."""
    cited = set(re.findall(r"`([a-zA-Z0-9_./-]+\.(?:py|md|sql))`", spec))
    cited |= set(re.findall(r"\]\((\.\./[a-zA-Z0-9_./-]+\.md)\)", spec))

    assert cited, "no citations found -- this test would pass vacuously"

    missing = []
    for path in sorted(cited):
        resolved = (SPEC.parent / path) if path.startswith("../") else (REPO / path)
        if not resolved.exists():
            missing.append(path)

    assert not missing, f"spec 0003 cites paths that do not resolve: {missing}"


def test_the_spec_is_marked_accepted_and_scoped_as_a_non_goal_list(spec):
    assert re.search(r"\*\*Status:\*\*\s*Accepted", spec)
    assert "## Non-goals" in spec, (
        "a governance spec without non-goals invites scope it never agreed to")
