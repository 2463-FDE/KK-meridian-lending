"""Seeded decision evidence must be present, internally coherent, and derived.

**WHAT THIS EXISTS TO STOP RECURRING.** A fresh `db/init` created 306
applications and 306 `decisions` rows and no `decision_events` at all, so every
underwriting screen reported -- correctly -- `Automated model decision: not
recorded`, `Model version: not recorded`, `Underwriting model score: not
recorded`. The UI was distinguishing `decisions.outcome` from `decision_events`
exactly as it should; the seed had created the first without the second, and the
screen built to demonstrate Reg B / ECOA evidence had none to show.

Filling it in was not available as a shortcut, and that is the part worth
keeping written down. `003_seed_bulk.sql` chose each outcome by rotating a
literal array on the application id, so the deterministic scorer reproduced only
135 of the 306 stored outcomes -- and all six hand-curated anchor applications
the demo points at were in the disagreeing group. An event saying "the model
decided approve" beside a score of 602, which this system's own bands call
`refer`, would have fabricated the audit record the table exists to hold.

So the seeded INPUTS were corrected and the outputs derived. Outcomes are
untouched; incomes and SSN-derived bureau tiers now land in the band their
outcome names. These cases hold that arrangement together.

**Three separate claims, deliberately not merged into one "the seed is fine":**

1. every event is DERIVED -- its score is the shipped scoring function of its
   own recorded inputs, not a number chosen to fit;
2. every event is INTERNALLY COHERENT -- its decision is the band its own score
   falls in, and it agrees with the application's final outcome;
3. every decided application HAS one -- the absence this started from cannot
   come back.

The last one is the guard the seed lacked. A future seed that adds an approved
application without evidence fails here rather than on a demo screen.
"""
import json
import pathlib
import re
import subprocess
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
EVENTS_SQL = REPO / "db" / "init" / "008_seed_decision_events.sql"
SEED_ANCHOR = REPO / "db" / "init" / "002_seed.sql"
SEED_BULK = REPO / "db" / "init" / "003_seed_bulk.sql"
TOOL = REPO / "db" / "tools" / "regenerate_seed_decision_events.py"
DECISION_SERVICE = REPO / "services" / "decision-service"

sys.path.insert(0, str(DECISION_SERVICE))
shipped = pytest.importorskip(
    "app.decision",
    reason="decision-service's own scorer is what these rows must be derived from")

#: One row of the generated block. Read positionally because the block is
#: generated with a fixed column list, which the header assertion below pins.
_ROW = re.compile(
    r"\(\s*(?P<app_id>\d+),\s*"
    r"'(?P<occurred_at>[^']+)',\s*"
    r"(?P<requested_amount>[\d.]+),\s*"
    r"(?P<term_months>\d+),\s*"
    r"(?P<annual_income>[\d.]+),\s*"
    r"(?P<bureau_score>\d+),\s*"
    r"(?P<model_score>\d+),\s*"
    r"'(?P<model_version>[^']*)',\s*"
    r"'(?P<top_features>\{[^']*\})'::jsonb,\s*"
    r"'(?P<decision>[a-z]+)',\s*"
    r"'(?P<reason_codes>\[[^']*\])'::jsonb\)")

_EXPECTED_COLUMNS = ("app_id, occurred_at, requested_amount, term_months, "
                     "annual_income, bureau_score, model_score, model_version, "
                     "top_features, decision, reason_codes")


def _band(score):
    """`_run_model`'s thresholds. Stated once; asserted against the code below."""
    if score >= 660:
        return "approve"
    return "deny" if score < 600 else "refer"


def _events():
    text = EVENTS_SQL.read_text(encoding="utf-8")
    out = []
    for m in _ROW.finditer(text):
        d = m.groupdict()
        out.append({
            "app_id": int(d["app_id"]),
            "occurred_at": d["occurred_at"],
            "requested_amount": float(d["requested_amount"]),
            "term_months": int(d["term_months"]),
            "annual_income": float(d["annual_income"]),
            "bureau_score": int(d["bureau_score"]),
            "model_score": int(d["model_score"]),
            "model_version": d["model_version"],
            "top_features": json.loads(d["top_features"]),
            "decision": d["decision"],
            "reason_codes": json.loads(d["reason_codes"]),
        })
    return out


def _seeded_outcomes():
    """`{app_id: outcome}` from both seed files, read rather than assumed."""
    outcomes = {}
    anchor = SEED_ANCHOR.read_text(encoding="utf-8")
    block = re.search(r"INSERT INTO decisions[^;]*?VALUES(?P<rows>[^;]*);",
                      anchor, re.S)
    if block:
        for m in re.finditer(r"\(\s*(\d+)\s*,\s*'([a-z]+)'\s*\)", block.group("rows")):
            outcomes[int(m.group(1))] = m.group(2)

    bulk = SEED_BULK.read_text(encoding="utf-8")
    rotation = re.search(
        r"INSERT INTO decisions \(app_id, outcome\)\s*"
        r"SELECT g, \(ARRAY\[(?P<array>[^\]]*)\]\)\[1 \+ \(\(g \* 2\) % 5\)\]\s*"
        r"FROM generate_series\((?P<first>\d+), (?P<last>\d+)\) g;",
        bulk, re.S)
    assert rotation, (
        "db/init/003_seed_bulk.sql no longer seeds decisions in the shape this "
        "test reads. It must fail rather than check only the anchor rows")
    choices = [v.strip().strip("'") for v in rotation.group("array").split(",")]
    for app_id in range(int(rotation.group("first")), int(rotation.group("last")) + 1):
        outcomes[app_id] = choices[(app_id * 2) % 5]
    return outcomes


def test_the_generated_block_is_still_there_and_parses():
    """Guard the guard.

    Every case below iterates `_events()`. If the block were renamed, reshaped
    or emptied they would all pass over nothing -- the vacuous pass this
    repository has produced before.
    """
    assert EVENTS_SQL.is_file(), (
        "%s is missing. Seeded decision evidence is what this file holds; "
        "without it every underwriting screen reads 'not recorded' again"
        % EVENTS_SQL.relative_to(REPO))
    text = EVENTS_SQL.read_text(encoding="utf-8")
    assert _EXPECTED_COLUMNS in text, (
        "the generated INSERT's column list changed; the positional parser in "
        "this test would silently read the wrong fields")
    events = _events()
    assert len(events) >= 300, (
        "parsed only %d decision events out of %s -- expected one per seeded "
        "application" % (len(events), EVENTS_SQL.name))


def test_the_tool_and_the_committed_sql_agree():
    """The generated block must be what the generator currently produces.

    Same contract as `test_seed_offer_consistency.py`: the tool runs in
    check-only mode and exits non-zero on drift, so a hand edit to the SQL, or a
    change to the seed formulas the tool reproduces, fails here.
    """
    result = subprocess.run([sys.executable, str(TOOL)], cwd=str(REPO),
                            capture_output=True, text=True)
    assert result.returncode == 0, (
        "db/tools/regenerate_seed_decision_events.py reports drift:\n%s\n%s"
        % (result.stdout, result.stderr))


def test_the_band_thresholds_here_match_the_shipped_ones():
    """This test states 660/600 itself, so it has to be held to the code.

    A second copy of a threshold is how the repository's worst drift happened.
    Read out of `decision.py` rather than trusted.
    """
    source = (DECISION_SERVICE / "app" / "decision.py").read_text(encoding="utf-8")
    assert "model_score >= 660" in source, (
        "decision.py no longer approves at >= 660; the bands in this test are "
        "stale and every coherence assertion below is measuring the wrong thing")
    assert 'decision_outcome = "deny" if model_score < 600 else "refer"' in source, (
        "decision.py no longer denies below 600; the bands in this test are stale")


@pytest.mark.parametrize("event", _events(), ids=lambda e: str(e["app_id"]))
def test_every_event_score_is_derived_from_its_own_inputs(event):
    """Claim 1. The score is the shipped function of the recorded inputs.

    This is what makes the row evidence rather than decoration: a reader can
    recompute it. A score chosen to fit the outcome would pass the coherence
    check below and fail here.
    """
    expected = shipped._stub_model_score(event["bureau_score"], event["annual_income"])
    assert event["model_score"] == expected, (
        "application %d records model_score %d, but the shipped scorer returns "
        "%d for bureau %d and income %.0f"
        % (event["app_id"], event["model_score"], expected,
           event["bureau_score"], event["annual_income"]))


@pytest.mark.parametrize("event", _events(), ids=lambda e: str(e["app_id"]))
def test_every_event_decision_is_the_band_of_its_own_score(event):
    """Claim 2a. Internal coherence, which is the fabrication this prevents."""
    assert event["decision"] == _band(event["model_score"]), (
        "application %d records decision %r with model_score %d, which falls in "
        "the %r band. An event whose decision its own score does not produce is "
        "the fabricated audit record this seed work exists to avoid"
        % (event["app_id"], event["decision"], event["model_score"],
           _band(event["model_score"])))


@pytest.mark.parametrize("event", _events(), ids=lambda e: str(e["app_id"]))
def test_every_event_agrees_with_the_final_outcome(event):
    """Claim 2b. The automated decision matches the outcome it explains.

    Scoped to the SEED, which has no `manual_reviews` rows at all: nothing has
    overridden anything, so agreement is the only coherent state. A real
    divergence -- a manual review changing the final outcome after the model ran
    -- is legitimate and the UI is built to show it; that is a different
    situation from a seed whose two tables never agreed in the first place.
    """
    outcomes = _seeded_outcomes()
    assert event["app_id"] in outcomes, (
        "application %d has a decision event and no seeded decision"
        % event["app_id"])
    assert event["decision"] == outcomes[event["app_id"]], (
        "application %d: seeded outcome %r, automated decision %r. No seeded "
        "manual review explains a divergence, so this is the seed disagreeing "
        "with itself" % (event["app_id"], outcomes[event["app_id"]],
                         event["decision"]))


def test_every_decided_application_has_evidence():
    """Claim 3. The absence this all started from cannot come back.

    The specific defect: 306 decisions, 0 events, and a screen truthfully
    reporting nothing recorded. A future seed adding an approved application
    without evidence fails here.
    """
    outcomes = _seeded_outcomes()
    have = {e["app_id"] for e in _events()}
    missing = sorted(set(outcomes) - have)
    assert not missing, (
        "%d seeded applications carry a decision and no decision_event, so an "
        "underwriting screen shows 'Automated model decision: not recorded' for "
        "them: %s%s"
        % (len(missing), missing[:10], " ..." if len(missing) > 10 else ""))


def test_no_application_has_two_events():
    """One run per seeded application.

    `decision_events` is append-only by design and a real re-decision appends,
    so more than one row is legitimate at RUNTIME. In the seed it would mean the
    generator emitted a duplicate, and a screen reading "the" automated decision
    would have to choose between them.
    """
    ids = [e["app_id"] for e in _events()]
    duplicated = sorted({i for i in ids if ids.count(i) > 1})
    assert not duplicated, "duplicate seeded decision events for %s" % duplicated


@pytest.mark.parametrize("event", _events(), ids=lambda e: str(e["app_id"]))
def test_reason_codes_are_present_exactly_when_there_is_an_adverse_action(event):
    """An approval explains nothing; a denial or a refer must.

    `_run_model` returns no reason codes on an approval -- there is no adverse
    action -- and the shipped `_reason_codes` attribution otherwise. A seeded
    denial with an empty list would put an unexplained refusal in front of an
    applicant, which is the Reg B question this table answers.
    """
    if event["decision"] == "approve":
        assert event["reason_codes"] == [], (
            "application %d is an approval carrying reason codes %s; an approval "
            "has no adverse action to explain"
            % (event["app_id"], event["reason_codes"]))
        return
    expected = shipped._reason_codes(event["bureau_score"], event["annual_income"])
    assert event["reason_codes"] == expected, (
        "application %d records %s; the shipped attribution for bureau %d and "
        "income %.0f is %s"
        % (event["app_id"], event["reason_codes"], event["bureau_score"],
           event["annual_income"], expected))


@pytest.mark.parametrize("event", _events(), ids=lambda e: str(e["app_id"]))
def test_the_model_version_says_it_is_the_stub(event):
    """These rows are the development scorer's output and must say so.

    `top_features` is the give-away that makes this matter: the bureau/income
    attribution IS the stub's own scoring math, and a licensed vendor response
    carries no attributions at all -- `decision.py` records null for one.
    Labelling these rows with a bare vendor version would present the stub's
    arithmetic as evidence about a licensed model.
    """
    assert event["model_version"].endswith("-stub"), (
        "application %d records model_version %r. Seeded evidence comes from "
        "the deterministic development scorer; a version without `-stub` claims "
        "a licensed model produced it"
        % (event["app_id"], event["model_version"]))
    assert event["model_version"] == "%s-stub" % shipped.AI_MODEL_VERSION, (
        "application %d records %r but decision-service's stub now labels "
        "itself %r-stub"
        % (event["app_id"], event["model_version"], shipped.AI_MODEL_VERSION))
    assert event["top_features"].get("bureau_score") == event["bureau_score"], (
        "application %d's top_features disagree with its own bureau_score"
        % event["app_id"])


def test_the_seed_records_no_manual_review_that_these_events_would_contradict():
    """Why agreement is asserted above rather than divergence being tolerated.

    If the seed ever starts creating `manual_reviews`, the agreement assertion
    stops being the right claim for those applications -- a human overriding the
    model is legitimate and the screen is built to show both. This fails first,
    pointing at the assertion that needs to become conditional, instead of
    letting a legitimate divergence look like a seed defect.
    """
    for seed in (SEED_ANCHOR, SEED_BULK):
        text = seed.read_text(encoding="utf-8")
        assert "INSERT INTO manual_reviews" not in text, (
            "%s now seeds manual reviews. Revisit "
            "test_every_event_agrees_with_the_final_outcome: a manual override "
            "makes a divergence correct, and this suite currently treats any "
            "divergence as a defect" % seed.name)


def test_the_events_table_is_still_append_only():
    """A manual review must not be able to rewrite what the model recorded.

    The claim that "the original decision_event remains unchanged" rests on the
    trigger in `004_decision_events.sql`, not on nobody trying. Asserted here
    because this file is where the seeded evidence's durability is claimed.
    """
    ddl = (REPO / "db" / "init" / "004_decision_events.sql").read_text(encoding="utf-8")
    assert "decision_events is append-only" in ddl
    assert "BEFORE UPDATE OR DELETE ON decision_events" in ddl, (
        "the append-only trigger on decision_events is gone, so seeded and "
        "runtime evidence can both be rewritten in place")

# --- the event must describe the application it points at ---------------------
#
# Codex found application 6013 recording `requested_amount = 7500` while
# `applications.amount` says 8000. One row, one typo, and it existed because the
# generator RESTATED the six anchor applications in a literal table instead of
# reading them out of the seed. The tool now parses them; these cases are the
# check that would have caught it either way.
#
# Deliberately three fields, not one. `requested_amount`, `term_months` and
# `annual_income` are the whole of what the event claims the model was given, and
# an event whose inputs are not the application's inputs is not evidence about
# that application -- it is a plausible-looking record of a decision on some
# other loan. The score is derived from income, so a wrong income would have been
# caught by the derivation test; a wrong AMOUNT or TERM would not have been by
# anything.


def _seeded_applications():
    """`{app_id: {amount, term_months, income}}`, read from both seed files.

    The anchors are literal rows; the bulk rows are SQL arithmetic. Both are
    read here rather than assumed, and the bulk formulas are taken from the seed
    text itself so a change to them fails this test instead of silently
    redefining what "consistent" means.
    """
    out = {}

    anchor = SEED_ANCHOR.read_text(encoding="utf-8")
    m = re.search(
        r"INSERT INTO applications\s*\(([^)]*)\)\s*VALUES\s*(?P<rows>.*?);",
        anchor, re.S)
    assert m, "no anchor applications INSERT in 002_seed.sql"
    columns = [c.strip() for c in m.group(1).split(",")]
    for name in ("id", "amount", "term_months", "income"):
        assert name in columns, (
            "002_seed.sql's applications INSERT no longer names %r" % name)
    at = {name: columns.index(name) for name in ("id", "amount", "term_months", "income")}
    for row in re.finditer(r"\(([^()]*)\)", m.group("rows")):
        parts = [x.strip() for x in row.group(1).split(",")]
        if len(parts) != len(columns):
            continue
        out[int(parts[at["id"]])] = {
            "amount": float(parts[at["amount"]]),
            "term_months": int(parts[at["term_months"]]),
            "income": float(parts[at["income"]]),
        }

    bulk = SEED_BULK.read_text(encoding="utf-8")
    # The formulas, asserted present rather than assumed, then applied.
    assert "(1000 + ((g * 263) % 49000))" in bulk, (
        "003_seed_bulk.sql's amount formula changed; this test would compare "
        "against arithmetic the seed no longer uses")
    assert "(ARRAY[12,24,36,48,60])[1 + ((g * 3) % 5)]" in bulk, (
        "003_seed_bulk.sql's term formula changed")
    for app_id in range(7000, 7300):
        outcome = ("approve", "approve", "approve", "deny", "refer")[(app_id * 2) % 5]
        spread = app_id * 311
        if outcome == "approve":
            income = float(48_000 + (spread % 150_000))
        elif outcome == "refer":
            income = float(24_000 + (spread % 23_000))
        else:
            income = float(24_000 + (spread % 25_000))
        out[app_id] = {
            "amount": float(1_000 + ((app_id * 263) % 49_000)),
            "term_months": (12, 24, 36, 48, 60)[(app_id * 3) % 5],
            "income": income,
        }
    return out


@pytest.mark.parametrize("event", _events(), ids=lambda e: str(e["app_id"]))
def test_every_event_records_the_application_it_points_at(event):
    """The inputs the event claims must be the application's own."""
    apps = _seeded_applications()
    app = apps.get(event["app_id"])
    assert app is not None, (
        "decision event for application %d, which neither seed file creates"
        % event["app_id"])

    assert event["requested_amount"] == app["amount"], (
        "application %d requested %.2f; its decision event records %.2f. An "
        "event whose inputs are not the application's inputs is evidence about "
        "some other loan"
        % (event["app_id"], app["amount"], event["requested_amount"]))
    assert event["term_months"] == app["term_months"], (
        "application %d is a %d-month term; its decision event records %d"
        % (event["app_id"], app["term_months"], event["term_months"]))
    assert event["annual_income"] == app["income"], (
        "application %d records income %.2f; its decision event records %.2f -- "
        "and the score is derived from income, so the two disagreeing means one "
        "of them is not what the model saw"
        % (event["app_id"], app["income"], event["annual_income"]))


def test_the_application_side_of_that_comparison_was_actually_read():
    """Guard the guard.

    If the anchor INSERT stopped matching, `_seeded_applications` would return
    only the 300 bulk rows and the six anchors -- the ones the demo points at,
    and the ones the defect was in -- would pass by not being compared.
    """
    apps = _seeded_applications()
    assert len(apps) >= 306, (
        "parsed only %d seeded applications; the anchors or the bulk range are "
        "missing from the comparison" % len(apps))
    for anchor in (4471, 5582, 6011, 6012, 6013, 6014):
        assert anchor in apps, (
            "anchor application %d was not read, so its event is unchecked "
            "against it -- which is exactly how 6013 shipped wrong" % anchor)
