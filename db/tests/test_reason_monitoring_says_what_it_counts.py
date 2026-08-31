"""The adverse-action panel must not claim more than its query answers.

`reason_distribution.py` filters `decision_events.decision = 'deny'`. So every
figure on that panel is a count of MODEL DENIAL EVENTS -- not of adverse actions.
In the demo database those differ by a factor of thirty-three: 66 rows where
`decisions.outcome = 'deny'` against 2 where `decision_events.decision = 'deny'`.

PR #141 put the scope in writing in one qualifying sentence, and that sentence is
still there and still required. What it did not reach was the surrounding copy:

  * the empty state said "No decisions carrying an adverse-action outcome were
    recorded in this window" -- the emptiest possible screen making the broadest
    possible claim;
  * each card said "N adverse decisions";
  * the missing-reason figure said "No-reason decisions".

A reader who trusted any of the three would have taken a count of model events for
a count of adverse actions. This guard holds the copy to the query.

Static, over the page source, because it is a claim about wording rather than
about rendering -- the browser spec covers what a person is shown.
"""
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
ADMIN_PAGE = REPO / "frontend" / "app" / "admin" / "page.tsx"
DISTRIBUTION = (REPO / "services" / "origination-service" / "app"
                / "reason_distribution.py")


def _page() -> str:
    return ADMIN_PAGE.read_text(encoding="utf-8")


def _page_without_comments() -> str:
    """The copy a person reads. Comments explain the history deliberately."""
    text = _page()
    text = re.sub(r"\{/\*.*?\*/\}", " ", text, flags=re.DOTALL)
    text = re.sub(r"^\s*//.*$", " ", text, flags=re.MULTILINE)
    return text


def test_the_query_really_is_deny_only():
    """The premise the copy rests on, established rather than assumed.

    If this fails the module now counts something wider, and the copy this file
    pins is the thing that has become wrong -- go and widen the copy, not this
    assertion.
    """
    source = DISTRIBUTION.read_text(encoding="utf-8")
    match = re.search(r"_ADVERSE_OUTCOMES\s*=\s*\(([^)]*)\)", source)
    assert match, "_ADVERSE_OUTCOMES is no longer a tuple literal"
    outcomes = {o.strip().strip("\"'") for o in match.group(1).split(",") if o.strip()}
    assert outcomes == {"deny"}, (
        f"the distribution now counts {sorted(outcomes)}, not deny alone. The "
        "panel's copy says 'model denial events' and 'deny-only distribution' -- "
        "widen the copy in the same change, do not relax this assertion.")


def test_the_empty_state_does_not_claim_no_adverse_actions_occurred():
    """An empty result means no model denial EVENT, not no adverse action."""
    copy = _page_without_comments()
    assert not re.search(
        r"No decisions carrying an adverse-action outcome", copy, re.IGNORECASE), (
        "the empty state claims no adverse-action decision was recorded, which is "
        "wider than the deny-only query behind it")
    assert re.search(r"No automated model decision events recorded as denials",
                     copy), (
        "the empty state no longer says what it actually checked")


def test_no_figure_is_labelled_as_an_adverse_decision_count():
    """Each card counts model denial events, and must say so."""
    copy = _page_without_comments()
    assert not re.search(r"\{v\.decisions\}\s*adverse", copy), (
        "a per-version count is still labelled 'adverse decisions'")
    assert re.search(r"\{v\.decisions\}\s*model denial", copy), (
        "the per-version count no longer names what it counts")


def test_the_missing_reason_figure_names_events_too():
    copy = _page_without_comments()
    assert "No-reason decisions" not in copy, (
        "the missing-reason figure still reads as a count of decisions")
    assert "Denial events with no reason" in copy


def test_the_scope_sentence_from_pr_141_survives():
    """The qualifier is load-bearing and must not be dropped as redundant.

    Precise labels say what each FIGURE is. This sentence says what the whole
    panel excludes -- a denial recorded on manual review after the model referred
    is outside the distribution even though the model event carries reason codes.
    Nothing in the labels conveys that.
    """
    copy = _page_without_comments()
    assert "deny-only distribution" in copy
    assert re.search(r"manual review", copy, re.IGNORECASE)


def test_the_panel_still_refuses_fairness_language():
    """PR #134's rule, re-asserted here because this PR rewrote nearby copy.

    The client prohibited runtime protected-class data and inferred proxies, and
    a panel that drifted into fairness language would undo that decision in copy
    while the code stayed correct.
    """
    copy = _page_without_comments().lower()
    for banned in ("disparate impact", "fairness dashboard", "bias detected",
                   "protected class analysis", "four-fifths"):
        assert banned not in copy, f"the panel now uses fairness language: {banned!r}"
