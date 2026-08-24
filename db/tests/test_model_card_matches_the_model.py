"""The model card is a governed artefact, not trusted prose.

`docs/model_card.md` is the document a regulator reads first. It names a model
version, the files implementing the controls it describes, a route it advertises
to staff, and an owner who is told to update it when the model changes. Nothing
enforced any of that: change `AI_MODEL_VERSION` and the governance artefact
becomes false, silently, with every test still green.

Every comparable document here is guarded -- README, ARCHITECTURE, ROADMAP,
DEBT, both runbooks, both decks. This one was not, which is the same defect as a
policy publishing a rule no code applies: a claim with no mechanism behind it.

**What these tests pin, and what they deliberately do not.** They pin FACTS the
card asserts about this repository -- a version string, the existence of a
governance surface, the owner's update trigger. They do not pin prose. A test
that froze wording would fail on every honest edit and be deleted within a
month, taking the guard with it.

The route the card advertises is proved elsewhere, deliberately:
`origination-service/tests/test_model_card_route_is_registered.py` asks the
running FastAPI app whether the route exists. That test lives with the service
because that is where the app can be imported, and because grepping source for
a decorator proves text, not a served route (PR #62, MC-004).

The fairness check is written to expire correctly rather than to freeze today's
gap. The card records that no model-level fairness validation has been performed
(W8-5). If that changes, the card and its evidence must move together -- so the
test asks the repository which state is current and holds the card to it, in
both directions.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
CARD = REPO / "docs" / "model_card.md"
DECISION_CONFIG = REPO / "services" / "decision-service" / "app" / "config.py"

#: The ZIP outcome screen is NOT model validation -- the card says so itself --
#: so it must never be discovered as evidence that W8-5 closed.
#:
#: Excluded by EXACT REPOSITORY PATH, not by filename substring. Reviewed on
#: PR #62 (MC-002): a substring rule on "fair_lending" would also suppress a
#: legitimate future artefact such as `tests/test_fair_lending_model_scores.py`,
#: which is real model-level work wearing a name containing the excluded word.
#: Suppressing real evidence is the worse error, because it keeps the card's
#: "no validation" claim alive after the validation exists.
_OUTCOME_MONITOR_PATHS = frozenset({
    "services/origination-service/app/fair_lending.py",
    "services/origination-service/tests/test_fair_lending.py",
})

#: The vocabulary this repository actually uses for fairness work, rather than
#: one filename spelling. All of these appear in the card, the code or the brief.
_FAIRNESS_TERMS = ("fairness", "fair_lending", "fair-lending",
                   "disparate_impact", "disparate-impact",
                   "model_fairness", "model_score")

#: A card asserting the validation has NEVER happened. Matched as a CLAIM rather
#: than as one sentence -- the assertion is what matters, however it is phrased.
_ABSENCE_CLAIM = re.compile(
    r"no\s+(?:model[- ]level\s+)?(?:fairness|disparate[- ]impact)[^.]{0,80}?"
    r"(?:testing|validation|evaluation|analysis)[^.]{0,60}?"
    r"(?:has|have)\s+(?:ever\s+)?(?:been\s+)?(?:run|performed|done|carried out)",
    re.I | re.S)

#: A card asserting the validation HAS happened.
_PRESENCE_CLAIM = re.compile(
    r"(?<!no )(?:fairness|disparate[- ]impact)[^.]{0,60}?"
    r"(?:testing|validation|evaluation)[^.]{0,40}?"
    r"(?:has|have|was|were)\s+(?:been\s+)?(?:run|performed|completed|validated)",
    re.I | re.S)


def _card() -> str:
    return CARD.read_text(encoding="utf-8")


def _configured_model_version() -> str:
    """The default `AI_MODEL_VERSION` decision-service actually ships with.

    Read out of `config.py` rather than restated here. A constant in this file
    would be a third copy of the version -- the card, the config, and the test --
    and the test's copy is the one nobody would think to update, so it would
    quietly agree with itself while both real sources drifted.
    """
    source = DECISION_CONFIG.read_text(encoding="utf-8")
    match = re.search(
        r'AI_MODEL_VERSION\s*=\s*os\.getenv\(\s*"AI_MODEL_VERSION"\s*,\s*"([^"]+)"\s*\)',
        source)
    assert match, (
        "could not read the default AI_MODEL_VERSION out of "
        f"{DECISION_CONFIG.name} -- the extraction broke, and every version "
        "assertion below would pass vacuously"
    )
    return match.group(1)


def _model_fairness_evidence():
    """Repository artefacts that look like model-LEVEL fairness evidence.

    Deliberately a search rather than a hardcoded "no". W8-5 is open today; when
    it closes this returns something and the fairness test flips from "you may
    not claim it" to "then cite it, and drop the denial". The card and its
    evidence move together, which is the point.

    Matched on the repository's own fairness vocabulary across app modules and
    tests, then the known ZIP outcome-monitor files are removed BY EXACT PATH.
    Reviewed on PR #62 (MC-002): the first version matched only `*fairness*` in
    test filenames, so `test_model_disparate_impact.py` was invisible and the
    stale claim would have survived real work landing.
    """
    found = set()
    for pattern in ("services/*/app/*.py", "services/*/tests/*.py"):
        for path in REPO.glob(pattern):
            name = path.name.lower()
            if any(term in name for term in _FAIRNESS_TERMS):
                found.add(path)
    return sorted(p for p in found
                  if p.relative_to(REPO).as_posix() not in _OUTCOME_MONITOR_PATHS)


def _section(body: str, heading_pattern: str):
    """The body of the first heading matching `heading_pattern`, or None.

    Sliced to the next heading of the SAME OR HIGHER level, so a subsection
    cannot leak the parent's content and the parent cannot swallow the rest of
    the document. Returning None rather than the whole card is the fix for
    PR #62 MC-003: the old fallback let a mention anywhere in the file satisfy a
    check that was supposed to be about one section.
    """
    for match in re.finditer(r"^(#{2,6})\s+(.+)$", body, re.MULTILINE):
        level, title = len(match.group(1)), match.group(2)
        if not re.search(heading_pattern, title, re.I):
            continue
        rest = body[match.end():]
        nxt = re.search(r"^#{1,%d}\s+" % level, rest, re.MULTILINE)
        return rest[:nxt.start()] if nxt else rest
    return None


# --------------------------------------------------------------------------
# Guard the guards.
# --------------------------------------------------------------------------

def test_the_card_exists_and_has_content_to_check():
    """A missing or gutted card would satisfy several assertions vacuously."""
    assert CARD.is_file(), "docs/model_card.md is gone"
    body = _card()
    assert len(body) > 2000, f"the card is {len(body)} characters -- too thin to be the artefact"
    assert body.lstrip().startswith("# Model Card"), "the card lost its own title"


def test_the_version_extraction_works():
    """If this stops returning a version, every comparison below passes on air."""
    version = _configured_model_version()

    assert version, "no default model version was extracted"
    assert re.match(r"^[a-z0-9][a-z0-9.\-]+$", version), version


def test_the_section_slicer_stops_at_the_next_heading():
    """`_section` is load-bearing for Guard 4, so it is tested directly rather
    than only through the card it happens to parse today."""
    doc = "# T\n\n## Owner\n\nalpha\n\n## Other\n\nbeta\n"

    owner = _section(doc, r"owner")
    assert owner is not None
    assert "alpha" in owner
    assert "beta" not in owner, "the slice ran past the next peer heading"
    assert _section(doc, r"nothing-like-this") is None


# --------------------------------------------------------------------------
# Guard 1 -- the model the card describes is the model that is configured.
# --------------------------------------------------------------------------

def test_the_card_names_the_configured_model_version():
    """Change `AI_MODEL_VERSION` without touching the card and this fails.

    That is the whole point: the card's Owner section already tells a reader to
    update it when the version changes, and until now nothing made that
    instruction bind.
    """
    version = _configured_model_version()

    assert version in _card(), (
        f"decision-service defaults to model version {version!r}, which appears "
        f"nowhere in the model card. The governance artefact is describing a "
        f"different model than the one that would run."
    )


def test_the_card_does_not_name_a_second_conflicting_version():
    """A card naming two versions is worse than one naming none -- a reader
    cannot tell which is current, and both look authoritative."""
    version = _configured_model_version()
    family = version.split("-")[0]

    others = {m for m in re.findall(rf"{re.escape(family)}-[0-9][0-9a-z.]*", _card())
              if m != version}
    assert not others, (
        f"the card names {sorted(others)} alongside the configured {version!r}"
    )


# --------------------------------------------------------------------------
# Guard 3 -- honesty, written to expire rather than to freeze.
# --------------------------------------------------------------------------

def test_the_card_keeps_a_governance_status_surface():
    """The section that tells a reader what is NOT done.

    Asserted structurally, not by wording: a card can be rewritten freely, but
    deleting the place where open gaps are disclosed turns the artefact into
    marketing -- the exact failure the Week 8 brief was about.
    """
    headings = re.findall(r"^##+ (.+)$", _card(), re.MULTILINE)

    assert any(re.search(r"limitation|known gap|not yet|open", h, re.I) for h in headings), (
        f"the card has no limitations or governance-status section: {headings}"
    )


def test_the_card_claims_no_more_fairness_validation_than_exists():
    """The durable contract, in BOTH directions.

    No evidence -> the card may not claim validation happened, and must still
    disclose that it has not. Evidence -> the card must cite it AND must no
    longer carry the denial.

    That second half is what review found missing (PR #62, MC-001): citing the
    new test *inside* the old "has never been run" sentence satisfied a citation
    check while leaving the card false. A reader takes the stale sentence as the
    status, and an artefact asserting both says nothing.

    It never demands the limitation stay open -- closing W8-5 means updating the
    card, not deleting a guard.
    """
    body = _card()
    evidence = _model_fairness_evidence()

    if not evidence:
        claimed = _PRESENCE_CLAIM.search(body)
        assert claimed is None, (
            "the card claims fairness validation has been performed while no "
            "model-level fairness evidence exists in the repository: "
            f"{claimed.group(0)!r}" if claimed else ""
        )
        assert re.search(r"fairness|disparate[- ]impact", body, re.I), (
            "the card no longer mentions fairness at all while model-level "
            "validation is still absent -- the gap stopped being disclosed "
            "rather than being closed"
        )
        return

    cited = [p for p in evidence if p.name in body or p.stem in body]
    assert cited, (
        f"model-level fairness evidence now exists ({[p.name for p in evidence]}) "
        f"and the card cites none of it. Update the card -- this test moves with "
        f"the evidence, it does not hold the gap open."
    )
    stale = _ABSENCE_CLAIM.search(body)
    assert stale is None, (
        f"the card cites {[p.name for p in cited]} while still asserting that no "
        f"such validation has ever been run: {stale.group(0)!r}. Citing the "
        f"evidence and keeping the denial beside it is worse than either alone -- "
        f"a reader cannot tell which is current." if stale else ""
    )


def test_the_zip_screen_is_named_only_as_retired_and_prohibited():
    """The card used to have to say the ZIP screen was an outcome monitor and
    not model validation. The client removed the screen instead.

    Client decision, 2026-08-24: no protected-class collection, no approved
    proxy, and none may be created from ZIP or ZIP3. So the card may still name
    the screen -- the record of a reversal is worth keeping -- but only as
    something retired, never as a control that exists. A card that reintroduced
    it as present-tense fairness evidence would be advertising a control the
    client prohibited.
    """
    body = _card()
    if "fair_lending" not in body and "zip-analysis" not in body:
        return  # the card no longer mentions it at all, which is also fine

    assert re.search(r"retired|deleted|no longer registered|prohibit", body, re.I), (
        "the card names the ZIP screen without saying it is retired; it was "
        "removed on 2026-08-24 by client decision")

    # And it must not present any runtime fairness analysis as existing.
    for match in re.finditer(r"fair_lending|zip-analysis|ZIP3", body):
        window = body[max(0, match.start() - 320):match.end() + 320]
        assert re.search(r"retired|deleted|no longer|prohibit|was\s|removed",
                         window, re.I), (
            "the card mentions the ZIP screen in a passage that does not mark "
            f"it as gone: ...{window[280:460].strip()}...")

    # Whitespace-normalised: the sentence wraps in the document, and a check
    # that depends on where a paragraph happens to wrap is a check about
    # formatting rather than about the claim.
    flat = re.sub(r"\s+", " ", body)
    assert re.search(r"No runtime fairness analysis is permitted", flat, re.I), (
        "the card does not state the current rule -- that no runtime fairness "
        "evaluation is permitted at all")


# --------------------------------------------------------------------------
# Guard 4 -- the artefact has an owner, and the commitment lives THERE.
# --------------------------------------------------------------------------

def test_the_cards_monitoring_claim_matches_what_exists():
    """The card now says reason-code frequency is measurable. If that surface
    is ever removed, the claim becomes a false statement in a governance
    artefact -- which is the failure this file exists to prevent, just pointed
    at a different sentence.

    Deliberately two-way. It also fails if the card still calls reason-code
    frequency unbuilt while the surface exists, so the claim cannot lag the
    code in either direction.
    """
    card = CARD.read_text(encoding="utf-8")
    module = REPO / "services" / "origination-service" / "app" / "reason_distribution.py"
    router = (REPO / "services" / "origination-service" / "app" / "routers"
              / "applications.py").read_text(encoding="utf-8")

    claims_measurable = "Reason-code frequency is now measurable" in card

    if claims_measurable:
        assert module.is_file(), (
            "the card claims reason-code frequency is measurable, but "
            f"{module.name} does not exist")
        assert "fair-lending/reason-distribution" in router, (
            "the card claims a route that is not registered")
    else:
        assert not module.is_file(), (
            "reason_distribution.py exists but the card still does not mention "
            "it -- the card is behind the code")


def test_the_card_names_an_owner_and_an_update_trigger():
    """Scoped to the ownership section, with no whole-document fallback.

    Reviewed on PR #62 (MC-003): the old version fell back to the entire card
    when the heading was not matched exactly, so `AI_MODEL_VERSION` in the
    Vendor/version section satisfied a test about the owner's commitment. The
    commitment could disappear and the guard it underwrites would not notice.

    Heading matching is deliberately permissive (Owner / Ownership / Maintainer
    / Maintenance) so the card can be reorganised; the CONTENT requirement is
    strict.
    """
    body = _card()
    owner = _section(body, r"owner|ownership|maintain")

    assert owner is not None, (
        "the card has no ownership section. A governance artefact nobody owns "
        "is a file, and the update trigger Guard 1 enforces has nowhere to live."
    )
    assert "AI_MODEL_VERSION" in owner, (
        "the ownership section no longer names AI_MODEL_VERSION as an update "
        "trigger. It may still appear elsewhere in the card, which is exactly "
        "the false pass this scoping exists to prevent."
    )
