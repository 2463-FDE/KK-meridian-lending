"""No live document describes code that has been deleted.

Every finding this file guards was real and committed. The ZIP3 fair-lending
screen was deleted on 2026-08-24 (PR #78) and nine sentences across
`ARCHITECTURE.md` and `specs/0003` went on describing it in the present tense --
including one that said the disparity route "is already staff-only" when the
route is not registered at all. Gated and absent are different answers, and the
weaker one was on the page for a fortnight.

**Fenced history is not a finding, and that distinction is the whole design.**
This repository deliberately keeps superseded text rather than rewriting it: a
spec edited to look current destroys the record of what was previously specified,
and a deck rewritten to look current destroys the only thing it is evidence of.
So a sentence marked as history passes; a sentence stating the same thing as
current fails.

**Scope is a sentence, not a file.** A document may legitimately contain both a
live paragraph and a fenced one -- `specs/0003` does by design, since its section
2 is superseded while sections 1 and 4-7 stand. Judging the whole file would let
one fence launder every claim in it.

**Two lessons from the first version of this guard, both kept because they are
the difference between a guard people trust and one they delete.**

  * It listed `four-fifths` and `min_group_size` alongside the identifiers, and
    both produced false failures on honest text: the superseded blocks quote the
    rule they described, and `Disparity thresholds beyond four-fifths --
    CLIENT-BLOCKED` is a true live statement about a decision nobody has made.
    A concept can be discussed after its implementation is gone; an identifier
    cannot be named as present. Guarding concepts made this a tax on writing
    accurately about what was removed.
  * It matched substrings, so bare `NOTE_RATE_PCT` flagged `DEMO_NOTE_RATE_PCT`
    -- the configured variable that REPLACED it. The guard failed the runbook for
    naming the right thing.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

#: Documents that describe the CURRENT system. A reader takes an unmarked
#: sentence here as true today.
LIVE_DOCS = (
    "ARCHITECTURE.md",
    "README.md",
    "docs/runbook.md",
    "docs/ROADMAP.md",
    "docs/DEBT.md",
    "docs/model_card.md",
    "specs/0003-fair-lending-monitoring.md",
)

#: Identifiers that no longer exist, with the code fact that proves it. Each is
#: matched as a whole token -- see the module docstring for why.
DELETED = {
    "fair_lending.py": "deleted with the ZIP3 screen (PR #78, client decision 2026-08-24)",
    "zip-analysis": "the route is not registered at all, not merely gated",
    "NOTE_RATE_PCT": "deleted from disclosure-service/app/fees.py (PR #80); the configured rate is DEMO_NOTE_RATE_PCT",
    "OFFER_RATE_PCT": "deleted from both frontend pages (PR #80)",
}

# `dead-but-present` was here and came out, for the reason above rather than
# despite it. The runbook described servicing's retired `POST /lss/payments` that
# way, which is exactly the phrasing D2 argues against -- a present-but-dead
# money route is one deployment from live -- so the line was corrected. But the
# phrase is PROSE, not an identifier: a mutation restoring it survived, because
# the corrected paragraph explains the deletion and so reads as fenced, quite
# correctly. Keeping an entry this guard cannot enforce would be worse than not
# having it: a green run would imply coverage that is not there. The fact itself
# is asserted where it can be, by
# `servicing-service/tests/test_legacy_payments_route_is_retired.py`.

#: A scope carrying one of these is describing history and may name anything.
#: Deliberately broad: the cost of missing a fence is a false failure on honest
#: text, which is how a guard gets deleted.
_FENCED = re.compile(
    r"(?i)(superseded|withdrawn|deleted|retired|no longer|until \d{4}-\d{2}-\d{2}|"
    r"previously|used to|"
    r"this (?:cell|bullet|line|paragraph|section|sentence) (?:read|said)|"
    r"was deleted|not on `main`|is gone)")

_WORD_CHARS = set("_abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789")


def _names(scope: str, symbol: str) -> bool:
    """Whether `scope` names `symbol` as a symbol, not inside a longer one."""
    for match in re.finditer(re.escape(symbol), scope):
        before = scope[match.start() - 1] if match.start() else " "
        after = scope[match.end()] if match.end() < len(scope) else " "
        if before in _WORD_CHARS or after in _WORD_CHARS:
            continue
        return True
    return False


def _scopes(text: str):
    """Split a document into independently-judged scopes.

    A markdown table row splits per CELL: a cell has no full stop of its own, so
    splitting on periods would let a fence in one cell cover a claim in the next.

    A blockquote run is ONE scope, together with the non-empty line before it.
    This repository fences superseded text by quoting the original verbatim under
    a heading that says so, which puts the marker OUTSIDE the quoted lines --
    judging each `>` line alone reported the preserved original as a live claim,
    flagging text for being preserved, which is the opposite of the point.
    """
    lines = text.replace("\r\n", "\n").split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]

        if line.lstrip().startswith(">"):
            start = index
            while index < len(lines) and (lines[index].lstrip().startswith(">")
                                          or not lines[index].strip()):
                index += 1
            lead = start - 1
            while lead >= 0 and not lines[lead].strip():
                lead -= 1
            yield "\n".join(lines[max(lead, 0):index])
            continue

        if line.lstrip().startswith("|"):
            index += 1
            for cell in line.split("|"):
                if cell.strip():
                    yield cell
            continue

        # Ordinary prose is hard-wrapped, so a SENTENCE routinely spans several
        # lines. Judging line by line orphaned the marker in `*This sentence said
        # the field "backs the ZIP3-level
        # four-fifths-rule ... screen
        # (`fair_lending.py`)" until that decision.*` -- the fence landed on one
        # line and the identifier on the next, and the guard reported a correctly
        # fenced sentence as a live claim. So the paragraph is joined first and
        # split into sentences after.
        start = index
        while (index < len(lines) and lines[index].strip()
               and not lines[index].lstrip().startswith(("|", ">"))):
            index += 1
        paragraph = " ".join(lines[start:index]) if index > start else line
        if index == start:
            index += 1

        # `...` is not a sentence end, and neither is a decimal point.
        for piece in re.split(r"(?<!\.)\.(?!\.)(?=\s|$)|(?<=[?!])\s", paragraph):
            if piece.strip():
                yield piece


@pytest.mark.parametrize("doc", LIVE_DOCS)
def test_no_live_scope_names_deleted_code(doc):
    path = REPO / doc
    assert path.exists(), "%s no longer exists; update LIVE_DOCS" % doc

    offenders = []
    for scope in _scopes(path.read_text(encoding="utf-8")):
        if _FENCED.search(scope):
            continue
        for symbol, why in DELETED.items():
            if _names(scope, symbol):
                offenders.append("%s: %r names %r (%s)"
                                 % (doc, scope.strip()[:200], symbol, why))

    assert not offenders, (
        "a live scope describes deleted code:\n" + "\n".join(offenders)
        + "\n\nEither the sentence is wrong, or it is history and needs a marker "
          "saying so. Both are fine; an unmarked present-tense claim about "
          "deleted code is not")


def test_the_deleted_identifiers_really_are_deleted():
    """Guard the guard, in the direction that matters.

    If one of these came back, this file would forbid documenting code that
    exists -- worse than the defect it was written for, because the obvious fix
    would be to delete the guard.
    """
    still_present = []

    if (REPO / "services" / "origination-service" / "app" / "fair_lending.py").exists():
        still_present.append("fair_lending.py exists again")

    for path in (REPO / "services" / "origination-service" / "app").rglob("*.py"):
        if "tests" in path.parts:
            continue
        if "zip-analysis" in path.read_text(encoding="utf-8", errors="replace"):
            still_present.append("%s registers zip-analysis"
                                 % path.relative_to(REPO).as_posix())

    fees = REPO / "services" / "disclosure-service" / "app" / "fees.py"
    if re.search(r"^NOTE_RATE_PCT\s*=", fees.read_text(encoding="utf-8"), re.M):
        still_present.append("fees.NOTE_RATE_PCT is back")

    for page in ("frontend/app/apply/page.tsx",
                 "frontend/app/underwriting/[appId]/page.tsx"):
        if "OFFER_RATE_PCT" in (REPO / page).read_text(encoding="utf-8"):
            still_present.append("%s holds OFFER_RATE_PCT" % page)

    assert not still_present, (
        "this guard forbids documenting code that has come back:\n"
        + "\n".join(still_present)
        + "\n\nRemove the identifier from DELETED before re-adding the code, or "
          "the next person to document it truthfully gets a failing test")


def test_a_quoted_superseded_block_is_not_flagged():
    """The false failure the first version produced, as a test.

    The fence sits on the heading above the quoted lines, which is exactly how
    this repository preserves superseded text.
    """
    document = "\n".join([
        "**SUPERSEDED 2026-08-24 -- NOT CURRENT POLICY.** Kept verbatim below.",
        "",
        "> The screen groups applicants by ZIP3 and applies the four-fifths rule,",
        "> reporting groups smaller than `min_group_size` (`fair_lending.py`).",
        "",
    ])

    flagged = [scope for scope in _scopes(document)
               if not _FENCED.search(scope)
               and any(_names(scope, s) for s in DELETED)]

    assert flagged == [], (
        "a quoted superseded block was flagged as a live claim: %r" % flagged)


def test_the_replacement_variable_is_not_mistaken_for_the_deleted_one():
    """`DEMO_NOTE_RATE_PCT` contains `NOTE_RATE_PCT`. The runbook naming the
    configured variable must not fail for spelling the old one inside it."""
    live = "an operator against a non-default `DEMO_NOTE_RATE_PCT` gets a refusal"

    assert not _names(live, "NOTE_RATE_PCT")
    assert _names("it read `NOTE_RATE_PCT` once", "NOTE_RATE_PCT")


def test_an_unfenced_claim_about_deleted_code_is_caught():
    """The real sentence that was on the page: the disparity route described as
    staff-only, when it is not registered at all."""
    live = "Reason reporting is aggregate, and the zip-analysis route is already staff-only"

    assert not _FENCED.search(live)
    assert _names(live, "zip-analysis")
