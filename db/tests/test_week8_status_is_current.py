"""The Week 8 planning surface may carry history, but not two current answers.

Week 8's governance package (PRs #65-#68) landed inside a single day, and the
documents describing it did not keep up: `docs/ROADMAP.md` still called the week
"Not yet started -- plan only", still said a model card and a monitoring spec did
not exist, and spec 0003 still said no mapping layer stood between a vendor
reason code and the consumer notice -- three paragraphs above the sentence saying
the mapping seam closes exactly that. That is what this guards: not stale prose
in general, but a document giving one answer in one place and the opposite answer
in another, about controls a reader can open and check.

**What this deliberately does not do.** It does not freeze paragraphs. History is
the point of a roadmap and the repository rule is to fence it, not delete it -- so
a stale-sounding claim passes when its own scope marks it as historical
("Superseded", "as of <date>", "this row read ... until", "before #66"). What
fails is an unfenced claim contradicting an artefact that is on `main`.

Scope of a claim: a table cell is its own scope, because a marker in the next
cell is not a marker on this one; ordinary prose is scoped to its paragraph,
because markdown wraps sentences across lines and a line-scoped check would
demand the marker land on the same physical line.

**Known limit, stated rather than papered over.** A stale claim written *inside*
a scope that legitimately fences history is not caught -- a cell whose job is to
say "before #66 no mapping layer existed" will shelter a fresh "no mapping layer
exists" written beside it. Sentence-level scoping would catch that and would
also fire on every ordinary edit, so the load-bearing claims are covered
positively instead: the section must name the artefacts, the route and the
tests, and must carry a dated delivered status. Mutation testing is how both the
cell-scope rule and this limit were found.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
ROADMAP = REPO / "docs" / "ROADMAP.md"
SPEC = REPO / "specs" / "0003-fair-lending-monitoring.md"
MODEL_CARD = REPO / "docs" / "model_card.md"
DECISION = REPO / "services" / "decision-service" / "app" / "decision.py"
DISTRIBUTION = (REPO / "services" / "origination-service" / "app"
                / "reason_distribution.py")
APPLICATIONS = (REPO / "services" / "origination-service" / "app" / "routers"
                / "applications.py")

#: A scope that says out loud it is describing the past. Kept deliberately
#: various: the repository fences history several ways already, and a guard that
#: recognised only one of them would push every future edit into one phrasing.
_HISTORICAL = re.compile(
    r"(?i)(historical|superseded|as of \d{4}-\d{2}-\d{2}|"
    r"\bbefore\s+(?:PR\s+)?#\d+|"
    r"until (?:those|that|it) landed|until \d{4}-\d{2}-\d{2}|"
    r"when this (?:row|section|spec|paragraph) was written|previously|"
    r"this (?:row|paragraph|section|clause) (?:read|used to)|"
    r"no longer live status|dated (?:discovery )?evidence|"
    r"kept as the record|day this spec was accepted|since acceptance|"
    r"stopped being true|closed since|"
    # Each week's "What client handed over" block records the artefacts as they
    # arrived at the start of the week. It is a handover inventory, past by
    # construction -- "No model card" there is a statement about what the client
    # supplied, not a claim about what the repository holds now.
    r"what client handed over)")


def _read(path: pathlib.Path) -> str:
    assert path.is_file(), f"missing: {path}"
    return path.read_text(encoding="utf-8")


def _scopes(text: str):
    """Every claim scope in `text`: table cells alone, prose by paragraph.

    A table row is not a small enough scope. These roadmap tables put the 2026
    discovery ("no model card") in one cell and the current status in the next,
    so a marker earned by the finding column would excuse anything written in
    the status column -- which is exactly the pair of cells that went stale.
    Mutation-testing caught it: a re-added "No model card exists yet" in the
    status cell passed while the row carried a marker. So each cell stands on
    its own.
    """
    for block in text.split("\n\n"):
        rows = [line for line in block.splitlines() if line.lstrip().startswith("|")]
        if rows:
            for row in rows:
                for cell in row.split("|"):
                    if cell.strip():
                        yield cell
            prose = "\n".join(line for line in block.splitlines()
                              if not line.lstrip().startswith("|"))
            if prose.strip():
                yield prose
        else:
            yield block


def _flat(scope: str) -> str:
    """The scope with its line wrapping removed.

    Markdown wraps prose mid-sentence, so both a claim and the marker fencing it
    routinely straddle a newline ("Not yet started -- plan\\nonly"). Matching the
    raw text would make every check depend on where the paragraph happened to
    wrap, which is the brittleness this file is trying to avoid.
    """
    return re.sub(r"\s+", " ", scope)


def _assert_only_historical(text, patterns, label):
    for pattern in patterns:
        for scope in (_flat(s) for s in _scopes(text)):
            if re.search(pattern, scope, re.I) and not _HISTORICAL.search(scope):
                pytest.fail(
                    f"{label}: {pattern!r} is stated as current, with nothing in "
                    f"its own scope marking it as history:\n{scope.strip()[:400]}")


@pytest.fixture(scope="module")
def week8() -> str:
    """The Week 8 section of the roadmap, which is its live planning surface."""
    text = _read(ROADMAP)
    start = text.index("## Week 8 —")
    end = text.index("## Week 9 —", start)
    return text[start:end]


@pytest.fixture(scope="module")
def spec() -> str:
    return _read(SPEC)


# --------------------------------------------------------------------------
# The artefacts the documents' current claims rest on. If one of these
# disappears, the checks below are asserting against nothing -- so they are
# verified rather than assumed.
# --------------------------------------------------------------------------

def test_the_week8_artefacts_exist():
    assert MODEL_CARD.is_file(), "docs/model_card.md is gone"
    assert re.search(r"\*\*Status:\*\*\s*Accepted", _read(SPEC)), (
        "spec 0003 is no longer Accepted")
    assert "def consumer_adverse_action_reason(" in _read(DECISION), (
        "the approved-mapping seam is gone")
    assert "def adverse_reason_distribution(" in _read(DISTRIBUTION), (
        "the section 1.3 reporting surface is gone")
    assert "/fair-lending/reason-distribution" in _read(APPLICATIONS), (
        "the reporting route is no longer registered")


def test_the_mapping_is_fixture_tested_without_a_live_model():
    tests = (REPO / "services" / "decision-service" / "tests"
             / "test_approved_consumer_reason.py")
    assert tests.is_file(), "the mapping's fixture tests are gone"
    assert "UnmappedAdverseActionReason" in _read(tests), (
        "nothing asserts an unmapped vendor code fails closed")

    # And the roadmap has to point at them. The brief's own remaining item was
    # "the mapping is not fixture-tested"; a reader closes that by opening the
    # file, so the citation is the evidence, not the prose around it. Without
    # this, a re-added "still not fixture-tested" sentence passes the negative
    # check by sitting in a row that carries a historical marker for an
    # unrelated clause.
    week8_text = _read(ROADMAP)
    week8_text = week8_text[week8_text.index("## Week 8 —"):
                            week8_text.index("## Week 9 —")]
    assert "test_approved_consumer_reason.py" in week8_text, (
        "the Week 8 section does not cite the mapping's fixture tests")


# --------------------------------------------------------------------------
# The roadmap's live Week 8 surface.
# --------------------------------------------------------------------------

def test_the_week8_section_does_not_call_delivered_work_unstarted(week8):
    _assert_only_historical(week8, [
        r"not yet started",
        r"plan only",
        r"pending go-ahead",
    ], "roadmap Week 8")


def test_the_week8_section_does_not_deny_the_artefacts_that_exist(week8):
    _assert_only_historical(week8, [
        r"no model card",
        r"a monitoring spec are not",
        r"monitoring spec (?:does not|doesn't) exist",
        r"not fixture-tested",
        r"disparity monitoring open",
    ], "roadmap Week 8")


def test_the_week8_section_states_a_current_delivered_status(week8):
    """The negative checks above are not sufficient on their own.

    A paragraph that legitimately quotes its own superseded wording carries a
    historical marker, and that marker then shelters anything else written in
    the same paragraph -- including a fresh "not yet started". Mutation-testing
    this file found exactly that. So the section must also say, positively and
    with a date, that the week is delivered and name what delivered it.
    """
    dated = [s for s in (_flat(x) for x in _scopes(week8))
             if re.search(r"Status \(\d{4}-\d{2}-\d{2}\)", s)]

    assert dated, "the Week 8 section carries no dated current-status statement"
    assert any("✅" in s and re.search(r"deliver", s, re.I) for s in dated), (
        "no dated status statement says the week is delivered")

    joined = " ".join(dated)
    for artefact in ("model_card.md", "0003-fair-lending-monitoring.md",
                     "consumer_adverse_action_reason", "reason_distribution.py"):
        assert artefact in joined, (
            f"the current-status statement does not name {artefact}, so a reader "
            f"cannot check the claim")


def test_the_week8_summary_row_agrees_with_the_week8_section():
    """The summary table near the top and the section far below are the two
    places a reader looks, and they used to disagree: the table said partial
    while the section said not started, and both were wrong."""
    row = next(line for line in _read(ROADMAP).splitlines()
               if line.startswith("| 8 |"))

    assert "✅" in row, f"the Week 8 summary row still reads as unlanded: {row}"
    for stale in ("\U0001f7e1", "⬜"):
        assert stale not in row, f"Week 8 summary row still carries {stale}: {row}"


def test_nothing_in_the_week8_section_is_left_bare_open(week8):
    """An item that cannot be closed here needs a reason in its own scope.

    "Still fully open" was true of model fairness and told a reader nothing
    about why it would stay that way. The stop condition for this week is that
    every row reads as closed, blocked by a named party, deferred, or history --
    never bare open.
    """
    # Deliberately only the labels that name who is blocking, or that put the
    # item outside this week. "Meridian must not claim it" explains the
    # consequence, not the blocker, and accepting it as a classification let a
    # declassified row pass under mutation.
    # Case-sensitive labels, because the lowercase words are ordinary prose:
    # "the consequence is stated rather than deferred" sheltered a declassified
    # row under mutation until this was tightened. A classification is a label,
    # so it has to look like one.
    classified = re.compile(
        r"(CLIENT-BLOCKED|VENDOR-BLOCKED|OPS-BLOCKED|DEFERRED|"
        r"[Nn]on-goal|out of scope for)")

    for scope in (_flat(s) for s in _scopes(week8)):
        for pattern in (r"still (?:fully )?open", r"⬜ *Open", r"\bTODO\b"):
            if re.search(pattern, scope, re.I):
                assert classified.search(scope) or _HISTORICAL.search(scope), (
                    f"Week 8 leaves {pattern!r} with no classification and no "
                    f"historical marker in its own scope:\n{scope.strip()[:300]}")


def test_an_unclosable_week8_item_is_classified_not_left_open(week8):
    """Model fairness cannot be closed here -- no protected-class evidence
    exists and none may be synthesised. That is a classification, not an open
    task, and the section has to say which."""
    assert "CLIENT-BLOCKED" in week8, (
        "the protected-class evidence limit is not classified")
    assert "VENDOR-BLOCKED" in week8, (
        "the vendor documentation/taxonomy limit is not classified")


# --------------------------------------------------------------------------
# Spec 0003's own current-state claims.
# --------------------------------------------------------------------------

def test_the_spec_does_not_say_the_mapping_seam_is_absent(spec):
    _assert_only_historical(spec, [
        r"no mapping layer",
        r"nothing reports how many",
        r"no windowed reporting is built",
    ], "spec 0003")


def test_the_spec_reports_the_landed_reporting_surface(spec):
    assert "reason_distribution.py" in spec, (
        "the spec does not name the module that answers its own section 1.3")
    assert "/fair-lending/reason-distribution" in spec, (
        "the spec does not name the route that answers its own section 1.3")


# --------------------------------------------------------------------------
# The two governance artefacts must not disagree about feature attribution.
# The spec had it backwards -- vendor-populated, stub-empty -- which is the
# inverse of both the code and the model card.
# --------------------------------------------------------------------------

def test_the_spec_and_the_card_agree_on_who_populates_top_features(spec):
    card = _read(MODEL_CARD)

    for label, text in (("spec 0003", spec), ("the model card", card)):
        attribution = [s for s in _scopes(text) if "top_features" in s]
        assert attribution, f"{label} no longer describes top_features at all"

        for scope in attribution:
            assert not re.search(r"populated by the vendor", scope, re.I), (
                f"{label} says the vendor populates top_features; the code "
                f"records null for a real vendor response:\n{scope.strip()[:300]}")

    assert re.search(r"only .{0,40}the deterministic stub", spec, re.I), (
        "spec 0003 no longer says top_features is stub-only")
    assert re.search(r"only populated for the dev/test stub", card, re.I), (
        "the model card no longer says top_features is stub-only")


def test_no_governance_document_claims_attribution_for_every_decision(spec):
    """"Records the features behind every decision" is the same error one
    sentence up from where it is contradicted.

    Spec 0003's context section said exactly that while its own §3 said
    `top_features` is stub-only -- review of this PR caught it. The claim is
    tempting because a *column* does exist on every row; what does not exist is
    a value in it for a vendor decision. So a document may pair "every
    decision" with feature attribution only if the same scope says which
    decisions actually carry it.
    """
    documents = {
        "spec 0003": spec,
        "the model card": _read(MODEL_CARD),
        "ADR 0006": _read(REPO / "adr" / "0006-adverse-action-reason-mapping.md"),
    }
    # Co-occurrence inside one scope, not proximity in characters. ADR 0006's
    # sentence puts "every decision" at the head of a column list and
    # `top_features` eleven columns later, which a distance-bounded pattern
    # missed -- mutation testing caught that. A paragraph or a cell is already a
    # tight enough scope to make co-occurrence meaningful.
    # `top_features` and "feature attribution" by name, plus the exact phrasing
    # review caught -- "records the model version and features behind every
    # decision", which names neither. A bare `\bfeatures\b` was tried and is too
    # broad at paragraph scope: it fires on an ADR bullet list where one bullet
    # says "every decision" and another says "features", which is not a claim
    # about attribution at all.
    attribution = re.compile(
        r"top_features|feature attribution|"
        r"features? (?:behind|for|driving|of) every decision", re.I)

    for label, text in documents.items():
        for scope in (_flat(s) for s in _scopes(text)):
            if attribution.search(scope) and re.search(r"every decision", scope, re.I):
                assert re.search(r"stub|null", scope, re.I), (
                    f"{label} pairs feature attribution with every decision and "
                    f"does not say which decisions carry it:\n{scope.strip()[:300]}")
