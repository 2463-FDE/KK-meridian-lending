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
# Vendor reason code is not consumer wording.
#
# This is the correction that arrived after the first draft. The spec had said
# vendor reason codes are "used verbatim" and had permitted, as one option for
# an unknown code, surfacing the vendor string unchanged. Traced through the
# code, that is not a hypothetical: `get_deny_reason` returns `reason_codes[0]`
# straight into `adverse_action_reason` with no mapping anywhere, so a vendor
# token like `high_debt_to_income` would reach a declined applicant. Permitting
# it in the spec would have promoted the defect to governed behaviour.
# --------------------------------------------------------------------------

def test_the_spec_separates_model_evidence_from_consumer_wording(spec):
    lowered = spec.lower()

    assert "model reason evidence" in lowered, (
        "the spec does not name the model-evidence artefact")
    assert "consumer adverse-action reason" in lowered, (
        "the spec does not name the consumer-facing artefact")
    assert re.search(r"not\W{1,4}automatically\W{1,4}authoritative", lowered), (
        "the spec does not say a vendor code is not automatically consumer "
        "wording, which is the whole distinction")


def test_the_spec_requires_an_unmapped_vendor_code_to_fail_closed(spec):
    assert re.search(r"unmapped vendor reasons? fail closed", spec, re.I), (
        "the fail-closed rule for unmapped codes is missing")

    # The specific escape hatch that was removed. If it comes back in any
    # permissive form, this fails.
    permissive = re.compile(
        r"(permitted|allowed|acceptable|may)[^.]{0,120}"
        r"surface[^.]{0,60}(vendor|raw)[^.]{0,60}unchanged", re.I)
    for match in permissive.finditer(spec):
        sentence_start = max(spec.rfind('.', 0, match.start()),
                             spec.rfind(chr(10) * 2, 0, match.start())) + 1
        end = spec.find('.', match.end())
        sentence = spec[sentence_start:(len(spec) if end == -1 else end + 1)]
        assert re.search(r'\b(not|never|wrong|must not|earlier draft)\b',
                         sentence, re.I), (
            f'spec 0003 permits raw vendor pass-through: {sentence.strip()!r}')


def test_the_spec_forbids_a_raw_machine_token_reaching_the_consumer(spec):
    assert "_node_finalize" in spec, (
        "the spec does not name the applicant-facing path that published the "
        "raw code; naming only the operational one understates it")
    assert "get_deny_reason" in spec, (
        "the spec does not name the function that currently passes the raw "
        "code through, so a reader cannot verify the problem exists")
    assert re.search(r"snake_case|machine (code|token)", spec, re.I)


def test_the_spec_does_not_promote_the_placeholder_into_an_approved_mapping(spec):
    """`high_debt_to_income` appears in this repo twice, both times as a test
    author's placeholder. Promoting it would invent a vendor taxonomy entry.

    Scoped to the containing PARAGRAPH. A wider window let a planted mapping
    entry pass by borrowing a warning from the paragraph above it -- the same
    masking mistake the fairness guard made, found the same way.
    """
    assert "high_debt_to_income" in spec, (
        "the placeholder is not named, so nothing warns against promoting it")

    warning = re.compile(r"placeholder|must not|not evidence|would put|never reach|specific reason", re.I)
    for match in re.finditer("high_debt_to_income", spec):
        blank = chr(10) * 2
        para_start = spec.rfind(blank, 0, match.start()) + 2
        para_end = spec.find(blank, match.end())
        paragraph = spec[para_start:(len(spec) if para_end == -1 else para_end)]
        assert warning.search(paragraph), (
            "high_debt_to_income appears in a paragraph that does not warn it "
            f"is not a real taxonomy entry: {paragraph.strip()[:160]!r}")


def test_the_spec_separates_blocked_mapping_content_from_buildable_mechanism(spec):
    """Both can be true at once, and saying so is what makes the follow-up
    actionable rather than parked behind the vendor."""
    assert re.search(r"CONTENT, not its MECHANISM|content.{0,40}mechanism",
                     spec, re.I), (
        "the spec does not separate the blocked mapping content from the "
        "buildable mapping mechanism")


def test_the_spec_requires_atomic_failure_and_preserved_provenance(spec):
    lowered = spec.lower()

    assert "partial committed state" in lowered or "partial" in lowered, (
        "nothing requires the fail-closed path to be atomic")
    assert re.search(r"provenance", lowered), (
        "nothing requires the raw code to survive as audit evidence")


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
