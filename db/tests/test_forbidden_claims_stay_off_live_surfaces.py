"""A claim we agreed not to make must not appear unqualified on a live screen.

The handoff deck carries a section called "Claims we must NOT make". Until now it
was a list somebody had to remember to re-read. This checks it.

**The list is read out of the deck, not copied here.** A copy would drift, and
the drift would be invisible: this file would keep passing against a list nobody
maintains. Adding a bullet to the deck extends this guard with no edit here.

**Qualified occurrences are allowed, and that is the whole subtlety.** The
landing page deliberately shows three inherited vendor badges -- SOX-controlled,
PCI compliant, ECOA / Reg B -- beside "Inherited vendor claims -- not verified by
Meridian". `docs/DEBT.md` D25 records why: they are the clearest artifact of the
platform over-claiming, and removing them silently would hide the history rather
than correct it. So the rule is not "this phrase may never appear". It is: **a
forbidden phrase must have the qualifier NEAR IT.**

Near it, not merely somewhere in the same file. Review finding FC-02: the first
version skipped any file containing the qualifier anywhere, so the landing page
-- the one file that legitimately carries it -- could have grown an unrelated
unqualified claim further down and never been inspected. The exception now
travels with the occurrence.

**Both sides are normalised before comparison** (FC-03). The deck is Markdown and
the surfaces are JSX, so a literal comparison misses ordinary renderings: a deck
phrase carrying `**emphasis**` would not match the same words authored plainly,
and JSX splits sentences across `{" "}`, tags and wrapped string literals. Both
texts are flattened to plain lowercase words before matching, so the guard is
about what a reader sees rather than how it was typed.

**What this does NOT do**, said plainly: it reads source, not a rendered page, so
a claim assembled at runtime from variables is out of reach.
`frontend/e2e/inherited-compliance-claims.spec.ts` covers the rendered landing
page. This covers every surface at unit speed -- breadth and speed here, fidelity
there.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
DECK = REPO / "docs" / "presentations" / "2026-08-25-agentic-client-handoff.md"

#: Where a reader can actually meet a claim. Tests and e2e specs are excluded --
#: a spec asserting a phrase is ABSENT has to name it.
SURFACES = [
    REPO / "frontend" / "app",
    REPO / "frontend" / "components",
]

#: The sentence that makes an inherited badge honest (D25), normalised.
QUALIFIER = "not verified by meridian"

#: How close the qualifier must sit to the claim it qualifies.
#:
#: A window rather than a JSX-container parse, and the limit is stated rather
#: than implied: this reads source text, so it cannot know that two strings
#: render inside the same element. 400 normalised characters is roughly a badge
#: row plus its surrounding markup -- close enough that a reader meets both
#: together, far enough that ordinary formatting does not split them.
QUALIFIER_WINDOW = 400

#: Claims currently in the deck that the derivation MUST find (FC-01).
#:
#: Pinned by name because a parser that quietly drops entries still returns a
#: plausible list, and a count-plus-canary check accepts it -- which is exactly
#: what happened. Each of these broke a real rule in the first version: the
#: first is 65 characters and a length cap dropped it; the next two are single
#: words and a word-count minimum dropped them.
MUST_BE_DERIVED = (
    "the payment-allocation placement is still an open client decision",
    "production-ready",
    "fully secure",
    "PCI compliant",
    "PCI certified",
    "SOC 2 compliant",
)


def _normalise(text: str) -> str:
    """Flatten Markdown or JSX to the words a reader would see.

    Emphasis markers, JSX string-splitting artifacts, tags, braces and runs of
    whitespace all disappear, so `settled **in the code**` and
    `settled{" "}in the code` both compare equal to `settled in the code`.
    """
    text = re.sub(r"\{\s*\"[^\"]*\"\s*\}", " ", text)    # {" "} and friends
    text = re.sub(r"<[^>]*>", " ", text)                  # JSX / HTML tags
    text = text.replace("&mdash;", " ").replace("&apos;", "'")
    text = re.sub(r"[*_`]", "", text)                     # markdown emphasis
    text = re.sub(r"[{}]", " ", text)
    text = re.sub(r"[—–]", " ", text)           # em / en dashes
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def _forbidden_phrases() -> list[str]:
    """Every claim quoted in a `Not ...` bullet of the deck's section 10.

    The convention is `- Not "X"`, sometimes chained as
    `- Not "X", "Y", "Z" or "W"`. Only the enumeration is taken: the prose after
    it explains WHY, and quotes things that are correct -- the
    payment-allocation bullet quotes "Captured -- allocation pending" as the
    wording that SHOULD be shown, and an earlier version of this parser duly
    flagged the component that renders it. A forbidden-claims list assembled by
    quoting everything nearby eventually forbids the right answer.

    No length cap and no word-count minimum. Both existed in the first version
    and both silently dropped real entries (FC-01).
    """
    text = DECK.read_text(encoding="utf-8")
    section = re.search(
        r"##\s*\d+\.\s*Claims we must NOT make(.*?)(?=\n##\s|\Z)", text, re.S
    )
    assert section, (
        "the handoff deck no longer has a 'Claims we must NOT make' section in "
        "the shape this guard reads. If it moved, point this file at it -- a "
        "guard that cannot find its subject passes for the wrong reason."
    )

    # Bullets, joined across their continuation lines so a claim that wraps is
    # still one string.
    bullets: list[str] = []
    for line in section.group(1).splitlines():
        if line.lstrip().startswith("- "):
            bullets.append(line.strip()[2:])
        elif bullets and line.strip():
            bullets[-1] += " " + line.strip()

    phrases: set[str] = set()
    for bullet in bullets:
        head = re.match(r'Not\s+"([^"]+)"', bullet)
        if not head:
            continue
        phrases.add(head.group(1))
        rest = bullet[head.end():]
        while True:
            nxt = re.match(r'\s*(?:,|or)\s+"([^"]+)"', rest)
            if not nxt:
                break
            phrases.add(nxt.group(1))
            rest = rest[nxt.end():]
    return sorted(phrases)


def _live_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for root in SURFACES:
        for path in root.rglob("*.tsx"):
            if "node_modules" in path.parts or ".next" in path.parts:
                continue
            files.append(path)
    return files


def test_the_deck_still_lists_claims_to_check():
    """Guard the guard, and not by counting.

    Review finding FC-01: a count plus a PCI canary accepted a 13-phrase list
    that had silently dropped three real claims. Naming them is what makes a
    dropped entry fail, instead of shrinking a number nobody checks.
    """
    phrases = _forbidden_phrases()

    missing = [p for p in MUST_BE_DERIVED if p not in phrases]
    assert not missing, (
        "the derivation no longer finds these claims, which ARE in the deck's "
        f"section 10: {missing}. Derived {len(phrases)}: {phrases}"
    )


def test_the_derivation_does_not_forbid_required_copy():
    """The opposite failure, just as bad and less obvious.

    Section 10 quotes correct wording while explaining a refusal -- "Captured --
    allocation pending" is the copy the payment receipt is REQUIRED to show. A
    parser that swept every quoted fragment forbade it, and would have failed
    the component that gets it right.
    """
    phrases = [p.lower() for p in _forbidden_phrases()]

    assert not any("allocation pending" in p for p in phrases), (
        "the derivation has started treating required receipt copy as a "
        "forbidden claim"
    )


def test_normalisation_sees_through_markdown_and_jsx():
    """FC-03, pinned directly rather than trusted.

    A deck phrase carrying `**emphasis**` and a page authoring the same words
    plainly must compare equal, and so must a sentence JSX has split.
    """
    assert _normalise("settled **in the code**") == "settled in the code"
    assert _normalise('settled{" "}in the code') == "settled in the code"
    assert _normalise("<span>PCI</span> compliant") == "pci compliant"


def test_the_guard_can_see_the_landing_page():
    files = _live_files()

    assert files, "no live surfaces found to sweep"
    assert any(f.name == "page.tsx" and f.parent.name == "app" for f in files), (
        "the landing page is not among the swept surfaces"
    )


def test_a_forbidden_claim_never_appears_without_a_nearby_qualifier():
    offences = []
    phrases = _forbidden_phrases()

    for path in _live_files():
        body = _normalise(path.read_text(encoding="utf-8", errors="replace"))
        for phrase in phrases:
            needle = _normalise(phrase)
            for match in re.finditer(re.escape(needle), body):
                window = body[
                    max(0, match.start() - QUALIFIER_WINDOW):
                    match.end() + QUALIFIER_WINDOW
                ]
                if QUALIFIER in window:
                    continue
                offences.append(
                    f"  {path.relative_to(REPO).as_posix()} says {phrase!r}"
                )
                break

    assert not offences, (
        "these live surfaces carry a claim the handoff deck says we must not "
        "make, with no 'inherited vendor claims -- not verified by Meridian' "
        "qualifier beside it:\n"
        + "\n".join(sorted(set(offences)))
        + "\n\nEither remove the claim or render it beside the qualifier "
        "(docs/DEBT.md D25)."
    )
