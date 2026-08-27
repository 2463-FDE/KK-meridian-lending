"""The SEC register has to keep the promises it made in prose.

The first is a coverage promise: the section's introduction says
`gateway/app/auth.py`'s docstring names three brownfield caveats and that all
three are mapped into the table. That sentence was written before the third one
was mapped, and it was true of the intention rather than of the table -- a
register whose introduction overstates its own coverage reproduces the exact
failure it exists to close, which is a caveat that is real, known, and visible
only to whoever opens the file it is written in.

The second is a marker promise: the footer says the rows whose conclusion
depends on running the stack carry `NEEDS RUNTIME VERIFICATION`. A footer that
promises a marker the table never uses is worse than no footer, because a reader
who cannot find the marker concludes there are no runtime-dependent rows rather
than that the marker was never applied.

The third is a range promise, and it is the one this file learned last: the
handoff note points a client reader at a span of SEC rows, and adding a row past
the end of that span silently drops it out of what the reader is told is tracked.
The change that added SEC-16 did exactly that, which is how the assertion got
written.

All three are wording, and wording is what goes stale. So all three are checked
here rather than left to a reviewer noticing each time.

What is deliberately NOT asserted: which rows carry the marker. That is a
judgement about evidence, and freezing it would turn a re-verified row into a
test failure. What is asserted is that the footer and the table agree on the
answer, whatever the answer is.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
DEBT = REPO / "docs" / "DEBT.md"
AUTH = REPO / "services" / "gateway" / "app" / "auth.py"

_MARKER = "NEEDS RUNTIME VERIFICATION"

#: The caveats `auth.py` keeps on purpose, and the words a SEC row would use to
#: map each one. Keyed by what the docstring says, valued by what the register
#: must show -- matching on substance rather than on a shared phrase, so
#: rewording either side does not quietly satisfy the check.
_CAVEATS = {
    "unsalted sha256 password hashing": (
        re.compile(r"sha256", re.I), re.compile(r"unsalted|salt", re.I)),
    "session tokens that never rotate": (
        re.compile(r"rotate", re.I), re.compile(r"token", re.I)),
    "the forwarded X-User-Role": (
        re.compile(r"X-User-Role"), re.compile(r"X-User-Role")),
}


def _debt() -> str:
    return DEBT.read_text(encoding="utf-8")


def _sec_section(debt: str = None) -> str:
    text = _debt() if debt is None else debt
    start = text.index("## Platform and perimeter security")
    end = text.index("## Not in this register", start)
    return text[start:end]


def _sec_rows(section: str) -> dict:
    """{'SEC-01': [cells]} for the register table, keyed by row id."""
    rows = {}
    for line in section.splitlines():
        m = re.match(r"\|\s*\*\*(SEC-\d+)\*\*\s*\|", line)
        if m:
            rows[m.group(1)] = [c.strip() for c in
                                re.split(r"(?<!\\)\|", line.strip())[1:-1]]
    return rows


#: A closed history note inside one status cell. Same narrow shape as the Week 9
#: guard (PR #107): no `re.S`, cannot cross an emphasis marker or a cell
#: boundary, and the closing `*` has to actually close something.
_HISTORY_NOTE = re.compile(r"\*This row read[^*|]*\*(?=\s|$)")


def _marked(rows: dict) -> set:
    """Row ids whose STATUS cell carries the marker as a LIVE claim.

    The status column specifically: the marker means "this row's conclusion is
    not yet runtime-proven", so a mention of the phrase in the What or Evidence
    column is prose about the marker, not the marker itself.

    History notes are stripped first, and that is not a nicety. When SEC-16 was
    verified and then fixed, its status cell quoted the wording it used to carry
    -- marker included -- so a plain substring test read the row as still marked
    and disagreed with a footer that had correctly dropped it. A guard that
    cannot tell a quotation from a claim reports the fix as the defect.
    """
    return {rid for rid, cells in rows.items()
            if _MARKER in _HISTORY_NOTE.sub("", cells[2])}


def _ids_cited(text: str) -> set:
    return set(re.findall(r"SEC-\d+", text))


def test_the_section_still_parses_as_a_table():
    """Every guard below is vacuous if the row parser stops matching."""
    rows = _sec_rows(_sec_section())

    assert len(rows) >= 15, (
        "only %d SEC rows parsed out of the register -- either the table's shape "
        "changed or the row pattern no longer matches it, and every assertion "
        "below would pass on an empty table" % len(rows))
    for rid, cells in rows.items():
        assert len(cells) == 5, (
            "%s has %d cells, not the table's five (ID / What / Status / "
            "Non-training requirement / Evidence)" % (rid, len(cells)))


def test_every_caveat_the_auth_docstring_keeps_is_mapped_into_the_register():
    """The introduction's coverage claim, checked against both documents.

    This is the finding that produced the file: the intro said three caveats
    were made visible in the register and the table carried two.
    """
    docstring = AUTH.read_text(encoding="utf-8").split('"""')[1]
    section = _sec_section()
    rows = _sec_rows(section)

    unmapped = []
    for caveat, (in_docstring, in_register) in _CAVEATS.items():
        if not in_docstring.search(docstring):
            pytest.skip("auth.py no longer names %r; the register is not "
                        "obliged to map a caveat that was removed" % caveat)
        if not any(in_register.search(" ".join(cells)) for cells in rows.values()):
            unmapped.append(caveat)

    assert unmapped == [], (
        "auth.py names these caveats and no SEC row covers them:\n  %s\nA caveat "
        "left in a docstring is invisible to the register-driven planning this "
        "section exists to enable -- map it to a row, or stop naming it as a "
        "known caveat." % "\n  ".join(unmapped))


def test_the_footer_and_the_table_agree_on_which_rows_are_runtime_bound():
    """The marker promise, in both directions.

    Promising the marker without applying it leaves a reader unable to separate
    static evidence from unverified conclusions. Applying it to rows the footer
    does not account for is the same defect facing the other way.
    """
    section = _sec_section()
    rows = _sec_rows(section)
    marked = _marked(rows)

    footer = section[section.index("**What NEEDS RUNTIME VERIFICATION means here.**"):]
    # Split on the sentence, not on the count inside it. The count changes every
    # time a row is added or a marker comes off, and a split token that goes
    # stale silently merges the two halves -- comparing the whole footer against
    # the marked set, which is how this assertion failed on a correct edit.
    parts = re.split(r"The other \w+ are not marked", footer, maxsplit=1)
    promised, settled = (parts[0], parts[1]) if len(parts) == 2 else (footer, "")

    assert marked, (
        "the footer explains a %s marker that no SEC row's status carries. A "
        "reader looking for the distinction it promises cannot find it."
        % _MARKER)

    assert _ids_cited(promised) == marked, (
        "the footer names %s as runtime-bound and the table marks %s"
        % (sorted(_ids_cited(promised)), sorted(marked)))

    assert _ids_cited(settled) == set(rows) - marked, (
        "the footer lists %s as settled by reading and the unmarked rows are %s"
        % (sorted(_ids_cited(settled)), sorted(set(rows) - marked)))


def test_a_statically_proven_row_is_not_marked_runtime_unknown():
    """The marker's other failure mode: over-application.

    Four rows are settled by reading a file that is in this repository --
    `sha256` in `auth.py`, `localStorage` in `api.ts`, `allow_origins=["*"]` in
    the gateway's `main.py`, and the absence of a `USER` directive. Running the
    stack does not make any of them more or less true. Marking them would dilute
    the marker into decoration, which is how a caveat stops being read.
    """
    rows = _sec_rows(_sec_section())
    marked = _marked(rows)

    static = {
        "SEC-01": "unsalted sha256 hashing is read straight out of auth.py",
        "SEC-04": "localStorage use is read straight out of the frontend",
        "SEC-06": "the absent USER directive is read out of nine Dockerfiles",
    }
    overmarked = ["%s -- %s" % (rid, why)
                  for rid, why in static.items() if rid in marked]

    assert overmarked == [], (
        "these rows are established statically and carry a runtime-unknown "
        "marker anyway:\n  %s" % "\n  ".join(overmarked))


def test_the_register_still_declines_to_rank_severity():
    """The section says it does not rank, and that has to keep being true.

    Adding a row is exactly when a severity word gets introduced -- a new
    finding feels like it needs one. This register's position is that ordering
    would need a data-flow audit nobody has done, so the status column says what
    is true instead.
    """
    section = _sec_section()
    rows = _sec_rows(section)

    assert re.search(r"No severity ranking here either", section), (
        "the section no longer states that it does not rank severity")

    banned = re.compile(r"\b(critical|high|medium|low)\s+severity\b"
                        r"|\bseverity[:=]\s*\w+", re.I)
    offenders = ["%s: %s" % (rid, m.group(0))
                 for rid, cells in rows.items()
                 for m in [banned.search(" ".join(cells))] if m]

    assert offenders == [], (
        "a severity label appeared in a register that says it has no severity "
        "system:\n  %s" % "\n  ".join(offenders))


def test_a_cited_sec_range_still_covers_every_row():
    """A range citation goes stale the moment a row is added past its end.

    Found by review on the change that added SEC-16: the handoff note pointed
    a client reader at `SEC-01`..`SEC-15`, so the row this PR existed to add
    fell outside the range it was supposedly tracked under. That is the
    citation rule this repository already enforces on paths, applied to the
    other thing a document can point at and miss.

    The alternative was to write `SEC-*` and let the range stop meaning
    anything. An explicit range says how much there is to read; a guard is
    what keeps it true.
    """
    rows = _sec_rows(_sec_section())
    highest = max(int(rid.split("-")[1]) for rid in rows)
    lowest = min(int(rid.split("-")[1]) for rid in rows)

    ranges = re.compile(r"SEC-(\d+)`?\s*(?:\.\.|--|—|–|-|to)\s*`?SEC-(\d+)")
    stale = []
    for doc in sorted((REPO / "docs").rglob("*.md")):
        for m in ranges.finditer(doc.read_text(encoding="utf-8")):
            first, last = int(m.group(1)), int(m.group(2))
            if first > lowest or last < highest:
                stale.append("%s: %s covers SEC-%02d..SEC-%02d, register holds "
                             "SEC-%02d..SEC-%02d"
                             % (doc.relative_to(REPO).as_posix(), m.group(0),
                                first, last, lowest, highest))

    assert stale == [], (
        "a document points a reader at a range of SEC rows that no longer "
        "covers the register:\n  %s" % "\n  ".join(stale))
