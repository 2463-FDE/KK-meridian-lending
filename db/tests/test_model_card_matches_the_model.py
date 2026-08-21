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
card asserts about this repository -- a version string, a route, the existence of
a governance surface, the owner's update trigger. They do not pin prose. A test
that froze wording would fail on every honest edit and be deleted within a month,
taking the guard with it.

The fairness check needs its own care, and it is written to expire correctly
rather than to freeze today's gap. The card currently records that no model-level
fairness validation has been performed (W8-5). That is true today. If it is ever
done, the right outcome is that the card and its evidence move together -- so the
test asks the repository whether such evidence exists and holds the card to
whichever answer is current. It never demands that the limitation stay open.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
CARD = REPO / "docs" / "model_card.md"
DECISION_CONFIG = REPO / "services" / "decision-service" / "app" / "config.py"
APPLICATIONS = REPO / "services" / "origination-service" / "app" / "routers" / "applications.py"

#: The outcome screen is NOT model validation, and the card says so itself. Any
#: search for model-fairness evidence has to exclude it, or the ZIP screen would
#: be read as closing a gap it explicitly does not close.
_OUTCOME_SCREEN = ("fair_lending", "zip-analysis", "zip_disparate_impact")


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
    match = re.search(r'AI_MODEL_VERSION\s*=\s*os\.getenv\(\s*"AI_MODEL_VERSION"\s*,\s*"([^"]+)"\s*\)',
                      source)
    assert match, (
        "could not read the default AI_MODEL_VERSION out of "
        f"{DECISION_CONFIG.name} -- the extraction broke, and every version "
        "assertion below would pass vacuously"
    )
    return match.group(1)


def _model_fairness_evidence():
    """Repository evidence that model-LEVEL fairness validation exists.

    Deliberately a search for artefacts rather than a hardcoded "no". W8-5 is
    open today; when it closes, this returns something and the fairness test
    below flips from "you may not claim it" to "then cite it". The card and its
    evidence move together, which is the point -- the guard must not become a
    reason to leave the gap open.

    The ZIP outcome screen is excluded on the card's own authority: it monitors
    recorded approvals, not the model's scores.
    """
    found = []
    for path in REPO.glob("services/*/tests/test_*fairness*.py"):
        if not any(marker in path.name for marker in _OUTCOME_SCREEN):
            found.append(path)
    for path in REPO.glob("services/*/app/*model_fairness*.py"):
        found.append(path)
    return sorted(found)


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
# Guard 2 -- what the card advertises, the code still provides.
# --------------------------------------------------------------------------

def test_the_fair_lending_route_the_card_advertises_exists():
    """The card sends a reader to a specific staff endpoint.

    Backticked FILE paths in the card are covered by the shared citation guard
    (`test_docs_citations_resolve.py`, which now includes this document). A
    ROUTE is not a path on disk, so it needs this: the card is the only place
    that advertises it, and a renamed route would leave the artefact directing
    an auditor at a 404.
    """
    body = _card()
    routes = re.findall(r"`GET (/[A-Za-z0-9/_{}-]+)`", body)
    assert routes, "the card advertises no route -- check the pattern before trusting this"

    source = APPLICATIONS.read_text(encoding="utf-8")
    prefix = re.search(r'APIRouter\(prefix="([^"]+)"', source)
    assert prefix, "could not read the router prefix"

    for route in routes:
        tail = route[len(prefix.group(1)):] if route.startswith(prefix.group(1)) else route
        assert f'@router.get("{tail}")' in source, (
            f"the card advertises {route}, which {APPLICATIONS.name} does not "
            f"register (looked for @router.get(\"{tail}\"))"
        )


# --------------------------------------------------------------------------
# Guard 3 -- honesty, written to expire rather than to freeze.
# --------------------------------------------------------------------------

def test_the_card_keeps_a_governance_status_surface():
    """The section that tells a reader what is NOT done.

    Asserted structurally, not by wording: a card can be rewritten freely, but
    deleting the place where open gaps are disclosed turns the artefact into
    marketing -- which is the exact failure the Week 8 brief was about.
    """
    headings = re.findall(r"^##+ (.+)$", _card(), re.MULTILINE)

    assert any(re.search(r"limitation|known gap|not yet|open", h, re.I) for h in headings), (
        f"the card has no limitations or governance-status section: {headings}"
    )


def test_the_card_claims_no_more_fairness_validation_than_exists():
    """The durable contract, in both directions.

    While no model-level fairness evidence exists in the repository, the card
    may not assert that it does. If such evidence is added, the card must cite
    it -- so closing W8-5 means updating the artefact, not fighting this test.

    Written this way on purpose: a test that permanently required the card to
    say "no fairness testing has been run" would make the gap unclosable
    without deleting the guard, and a guard that punishes progress gets deleted.
    """
    body = _card()
    evidence = _model_fairness_evidence()

    if not evidence:
        claimed = re.search(
            r"(fairness|disparate[- ]impact)[^.\n]{0,60}"
            r"(has been|have been|was|were)\s+(run|performed|completed|validated)",
            body, re.I)
        if claimed and not re.search(r"\bno\b[^.\n]{0,40}" + re.escape(claimed.group(0)[:20]),
                                     body, re.I):
            pytest.fail(
                "the card claims fairness validation has been performed, and no "
                f"model-level fairness evidence exists in the repository: "
                f"{claimed.group(0)!r}"
            )
        assert re.search(r"fairness|disparate[- ]impact", body, re.I), (
            "the card no longer mentions fairness at all while model-level "
            "validation is still absent -- the gap stopped being disclosed "
            "rather than being closed"
        )
    else:
        cited = [p for p in evidence if p.name in body or p.stem in body]
        assert cited, (
            f"model-level fairness evidence now exists ({[p.name for p in evidence]}) "
            f"and the card cites none of it. Update the card -- this test moves "
            f"with the evidence, it does not hold the gap open."
        )


def test_the_outcome_screen_is_not_presented_as_model_validation():
    """The distinction the card itself insists on, kept mechanical.

    The ZIP screen watches recorded approvals. Reading it as validation of the
    model's scores is the specific misreading the Week 8 brief warns about, and
    the card is where someone would acquire it.
    """
    body = _card()
    if "fair_lending" not in body and "zip-analysis" not in body:
        pytest.skip("the card no longer describes the ZIP outcome screen")

    assert re.search(r"not\s+(a\s+)?model validation|outcome monitor", body, re.I), (
        "the card describes the ZIP screen without saying it is an outcome "
        "monitor rather than model validation"
    )


# --------------------------------------------------------------------------
# Guard 4 -- the artefact has an owner and a trigger to update it.
# --------------------------------------------------------------------------

def test_the_card_names_an_owner_and_an_update_trigger():
    """Semantic anchors, not the sentence.

    A governance artefact nobody owns is a file. What matters is that some
    section identifies responsibility and names at least the model-version
    change as the event that requires an update -- which is precisely the
    trigger Guard 1 now enforces.
    """
    body = _card()
    headings = re.findall(r"^##+ (.+)$", body, re.MULTILINE)

    assert any(re.search(r"owner|ownership|maintain", h, re.I) for h in headings), (
        f"the card names no owner: {headings}"
    )
    owner = body.split("## Owner", 1)[-1] if "## Owner" in body else body
    assert "AI_MODEL_VERSION" in owner, (
        "the owner section no longer names AI_MODEL_VERSION as an update "
        "trigger, so the commitment Guard 1 enforces is no longer written down"
    )
