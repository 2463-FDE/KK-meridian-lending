"""A claim we agreed not to make must not appear unqualified on a live screen.

The handoff deck carries a section called "Claims we must NOT make". Until now
it was a list somebody had to remember to re-read. This checks it.

**The list is read out of the deck, not copied here.** A copy would drift, and
the drift would be invisible: this file would keep passing against a list nobody
maintains. Adding a bullet to the deck extends this guard with no edit here,
which is the same derivation the other guards in this directory use.

**Qualified occurrences are allowed, and that is the whole subtlety.** The
landing page deliberately shows three inherited vendor badges -- SOX-controlled,
PCI compliant, ECOA / Reg B -- beside the sentence "Inherited vendor claims --
not verified by Meridian". `docs/DEBT.md` D25 records why: they are the clearest
artifact of the platform over-claiming, and removing them silently would hide
the history rather than correct it. So the rule is not "this phrase may never
appear". It is: **if a forbidden phrase appears on a live surface, the qualifier
must appear on that surface too.**

`frontend/e2e/inherited-compliance-claims.spec.ts` already pins that relationship
in the browser, for the landing page. This pins it statically, for every surface,
at unit speed -- so a new page carrying "PCI compliant" without the qualifier
fails here rather than waiting for somebody to open it.

**Comments count as surfaces, deliberately.** Several files discuss these phrases
in prose explaining why they are refused; those files carry the qualifier or the
refusal in the same breath, which is exactly the condition this asserts. A file
that names a forbidden claim with nothing nearby saying it is not ours is the
defect, whether the words are in JSX or in a comment above it.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
DECK = REPO / "docs" / "presentations" / "2026-08-25-agentic-client-handoff.md"

#: Where a reader can actually meet a claim: rendered pages and components.
#: Tests and e2e specs are excluded -- a spec asserting a phrase is ABSENT has
#: to name it, and flagging that would make the guard unusable.
SURFACES = [
    REPO / "frontend" / "app",
    REPO / "frontend" / "components",
]

#: The sentence that makes an inherited badge honest (D25). Matched loosely on
#: its load-bearing half so a wording tweak does not silently disarm the guard.
QUALIFIER = re.compile(r"not verified by Meridian", re.I)

#: Phrases short enough to be searched for literally. The deck's bullets are
#: prose; only the quoted fragments are claims, and only some of those are
#: distinctive enough to grep without matching ordinary text.
_MIN_PHRASE_WORDS = 2


def _forbidden_phrases() -> list[str]:
    """The quoted claims from the deck's "Claims we must NOT make" section."""
    text = DECK.read_text(encoding="utf-8")
    section = re.search(
        r"##\s*\d+\.\s*Claims we must NOT make(.*?)(?=\n##\s|\Z)", text, re.S
    )
    assert section, (
        "the handoff deck no longer has a 'Claims we must NOT make' section in "
        "the shape this guard reads. If it moved, point this file at it -- a "
        "guard that cannot find its subject passes for the wrong reason."
    )
    # Only what follows `Not`, which is the section's convention:
    #
    #   - Not "PCI compliant" or "PCI certified".
    #
    # Taking EVERY quoted fragment was the first attempt and it was wrong: the
    # bullets also quote copy that is REQUIRED -- "Captured — allocation
    # pending" appears inside the payment-allocation bullet as the wording that
    # should be shown -- and the guard promptly flagged the component that
    # correctly renders it. A list of forbidden claims assembled by quoting
    # everything nearby will eventually forbid the right answer.
    body = section.group(1)
    phrases: set[str] = set()
    for line in body.splitlines():
        head = re.search(r'Not\s+"([^"]{4,60})"', line)
        if not head:
            continue
        phrases.add(head.group(1))
        # `... or "PCI certified"` chains on the same bullet.
        phrases.update(re.findall(r'or\s+"([^"]{4,60})"', line))
    return sorted(
        p.strip().strip(".") for p in phrases
        if len(p.split()) >= _MIN_PHRASE_WORDS
    )


def _live_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for root in SURFACES:
        for path in root.rglob("*.tsx"):
            if "node_modules" in path.parts or ".next" in path.parts:
                continue
            files.append(path)
    return files


def test_the_deck_still_lists_claims_to_check():
    """Guard the guard: an empty list makes the assertion below vacuous."""
    phrases = _forbidden_phrases()

    assert len(phrases) >= 5, (
        f"only {len(phrases)} forbidden phrases parsed out of the deck: "
        f"{phrases}. The section shape probably changed."
    )
    # The one everybody knows, as a canary that parsing worked at all.
    assert any("PCI" in p for p in phrases), phrases


def test_the_guard_can_see_the_landing_page():
    """And that it is looking at real files.

    The landing page is the surface that legitimately carries the badges, so if
    the sweep cannot see it, the interesting case is not being checked.
    """
    files = _live_files()

    assert files, "no live surfaces found to sweep"
    assert any(f.name == "page.tsx" and f.parent.name == "app" for f in files), (
        "the landing page is not among the swept surfaces"
    )


def test_a_forbidden_claim_never_appears_without_its_qualifier():
    offences = []
    phrases = _forbidden_phrases()

    for path in _live_files():
        body = path.read_text(encoding="utf-8", errors="replace")
        if QUALIFIER.search(body):
            # The inherited-badge case: the claim and the sentence that says it
            # is not ours live together, which is what D25 requires and what
            # inherited-compliance-claims.spec.ts pins in the browser.
            continue
        for phrase in phrases:
            if re.search(re.escape(phrase), body, re.I):
                offences.append(
                    f"  {path.relative_to(REPO).as_posix()} says {phrase!r}"
                )

    assert not offences, (
        "these live surfaces carry a claim the handoff deck says we must not "
        "make, with nothing on the same surface saying it is an inherited "
        "vendor claim not verified by Meridian:\n"
        + "\n".join(sorted(offences))
        + "\n\nEither remove the claim or render it beside the qualifier "
        "(docs/DEBT.md D25)."
    )
