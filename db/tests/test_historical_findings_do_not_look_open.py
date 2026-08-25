"""A dated finding must not wear the marker that means "open work".

`docs/ROADMAP.md` deliberately preserves discovery evidence rather than
rewriting it: a spec or a status block edited to look current destroys the
record of what was actually found, so the original wording stays and a fence
beside it says what has since closed. That convention is right and this file
does not change it.

What it caught is narrower. Week 7's four discovery rows read:

    | ⬜ Open as of 2026-08-05 | ...

`⬜` is the legend's marker for **open work**, so anyone scanning the document
for outstanding items counted those four -- and the prose fence, which sits
*below* the table, only helps a reader who reads downward. I did exactly that
while auditing this repository and reported six open Week 7 items to the client
when two were open and four had been closed for weeks.

The fence was correct. The glyph was not. So dated findings now carry `🕒`, and
this asserts that no dated finding wears `⬜` again.

**Deliberately mechanical, and deliberately narrow.** It does not try to decide
whether a block is "historical" from its prose -- that would need the fence
detection this file exists because nobody performs by eye. It uses the one
signal that is unambiguous in a table cell: a status that names a PAST DATE is a
dated measurement, not a live status, whatever else it says.
"""
import datetime
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
ROADMAP = REPO / "docs" / "ROADMAP.md"

#: The legend's marker for work that is open NOW.
OPEN_MARKER = "⬜"

#: The marker for a dated finding that is no longer live status.
HISTORICAL_MARKER = "\U0001f552"

#: A status cell that dates itself: "as of 2026-08-05", "on 2026-08-05",
#: "2026-08-05". Any of these makes the cell a measurement rather than a claim
#: about today.
_DATED = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")

#: Wording that says the date is when something was FIXED rather than when it was
#: found. "Closed 2026-08-24" is a dated claim about the present and may sit
#: beside any marker; it is the dated OPEN findings this file is about.
_CLOSURE_WORDING = re.compile(
    r"(?i)(closed|landed|fixed|delivered|merged|resolved|since|superseded|"
    r"decision received|implemented)")


def _status_cells():
    """Every status cell in the document, with its line number.

    Column 4 by position is where the tables put status, but the tables in this
    file are not uniform -- some have five columns, some three. So every cell of
    every row is yielded and the marker itself is what identifies a status,
    rather than a column index that would silently start reading the wrong field.
    """
    for lineno, line in enumerate(ROADMAP.read_text(encoding="utf-8").splitlines(), 1):
        if not line.lstrip().startswith("|"):
            continue
        for cell in line.split("|"):
            if cell.strip():
                yield lineno, cell.strip()


def test_no_dated_finding_wears_the_open_marker():
    """The defect itself: `⬜ Open as of <past date>`.

    A cell that carries the open marker AND a date AND no closure wording is
    claiming to be open work while describing a measurement taken on a
    particular day. One of those two has to go, and the answer is the marker --
    the date is the honest part.
    """
    offenders = []
    for lineno, cell in _status_cells():
        if OPEN_MARKER not in cell:
            continue
        if not _DATED.search(cell):
            continue
        if _CLOSURE_WORDING.search(cell):
            # e.g. "⬜ Open -- unchanged since 2026-08-05" is a live claim that
            # happens to cite a date. Not this file's business.
            continue
        offenders.append("docs/ROADMAP.md:%d  %s" % (lineno, cell[:160]))

    assert not offenders, (
        "a dated finding is wearing %s, the marker for work that is open now:\n%s"
        "\n\nUse %s for a dated finding. A reader scanning for open items counts "
        "%s, and a prose fence below the table does not reach them -- which is "
        "exactly how four closed Week 7 findings were reported to a client as "
        "outstanding." % (OPEN_MARKER, "\n".join(offenders), HISTORICAL_MARKER,
                          OPEN_MARKER))


def test_the_historical_marker_is_defined_in_the_legend():
    """A marker nobody defines is a glyph, not a status.

    The legend is the only place a reader can find out what these mean, and the
    previous version of this document defined five markers and used six.
    """
    text = ROADMAP.read_text(encoding="utf-8")
    legend = text[: text.index("Every `D<n>`")]

    assert HISTORICAL_MARKER in legend, (
        "%s is used in the roadmap but not defined in its status legend"
        % HISTORICAL_MARKER)
    assert OPEN_MARKER in legend, "the open marker is no longer defined either"


def test_the_historical_marker_is_actually_used():
    """Guard against the lazy fix.

    Deleting the four rows, or dropping their dates, would satisfy the check
    above and lose the discovery evidence -- which is the thing the convention
    exists to keep. So at least one dated finding must still be recorded, and
    recorded as history.
    """
    text = ROADMAP.read_text(encoding="utf-8")

    assert text.count(HISTORICAL_MARKER) >= 2, (
        "no dated findings are recorded with %s any more. Preserving them is the "
        "point: a document rewritten to look current cannot show what was found"
        % HISTORICAL_MARKER)


def test_the_week_7_discovery_rows_are_still_there_and_still_dated():
    """The specific evidence this whole convention was protecting.

    Week 7's four rows are the client's own reported symptom -- "payments feel
    flaky", month-end noise written off -- and what was actually found behind
    it. They are the most valuable paragraphs in the file and the easiest to
    delete while "tidying up" a marker.
    """
    text = ROADMAP.read_text(encoding="utf-8")

    for evidence in ("ledger_total()", "settlement_total()", "5582",
                     "processor_ref"):
        assert evidence in text, (
            "Week 7's discovery evidence no longer mentions %r; the findings were "
            "rewritten rather than fenced" % evidence)

    # And they are still dated, so nobody can read them as current.
    assert text.count("%s Was open on 2026-08-05" % HISTORICAL_MARKER) >= 4


# --- guard the guard ----------------------------------------------------------


@pytest.mark.parametrize("cell,should_flag", [
    ("⬜ Open as of 2026-08-05", True),
    ("⬜ Open as of 2026-08-05, and verified directional", True),
    ("\U0001f552 Was open on 2026-08-05", False),
    ("⬜ Open", False),
    ("⬜ Open -- unchanged since 2026-08-05", False),
    ("✅ Closed 2026-08-24", False),
    ("⬜ Open (spec this week, not built)", False),
])
def test_the_rule_flags_what_it_should_and_nothing_else(cell, should_flag):
    """The rule stated as cases, because a regex over prose is easy to write so
    loosely it matches everything or so tightly it matches nothing.

    The fifth case is the one that keeps this honest: a genuinely open item may
    cite a date to say how long it has been open, and flagging that would push
    people to strip dates from live status -- the opposite of what this file
    wants.
    """
    flagged = (OPEN_MARKER in cell
               and bool(_DATED.search(cell))
               and not _CLOSURE_WORDING.search(cell))

    assert flagged is should_flag, (
        "the rule %s %r" % ("flagged" if flagged else "did not flag", cell))


def test_every_date_in_a_flagged_shape_is_in_the_past():
    """A sanity check on the premise.

    The rule assumes a dated status cell describes a measurement already taken.
    A future date would mean something else entirely -- a plan, a deadline -- and
    this file should not be quietly reinterpreting one.
    """
    today = datetime.date.today()
    for lineno, cell in _status_cells():
        for match in _DATED.finditer(cell):
            when = datetime.date.fromisoformat(match.group(0))
            assert when <= today, (
                "docs/ROADMAP.md:%d carries a FUTURE date %s in a status cell; "
                "this file's rules are about dated findings, not deadlines, so "
                "the assumption behind them no longer holds"
                % (lineno, when))
