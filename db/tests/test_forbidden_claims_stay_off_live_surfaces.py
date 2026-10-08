"""A claim we agreed not to make must not appear unqualified on a live screen.

The client handoff agreed a list of claims we must not make. A list somebody
has to remember to re-read is not a control, so it lives here, in
`FORBIDDEN_CLAIMS`, and every live surface is checked against it. Adding a claim
to that tuple extends the guard.

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

**Both sides are normalised before comparison** (FC-03). The surfaces are JSX,
so a literal comparison misses ordinary renderings: JSX splits sentences across
`{" "}`, tags and wrapped string literals, and a phrase may carry Markdown
emphasis. Both texts are flattened to plain lowercase words before matching, so
the guard is about what a reader sees rather than how it was typed.

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

#: Claims no live surface may make without the D25 qualifier beside it.
#:
#: Several are engagement facts rather than product features -- the payment
#: allocation placement and the fairness policy were client decisions -- and
#: they stay because a screen asserting the retired version would be telling a
#: reader something false.
FORBIDDEN_CLAIMS = (
    "PCI compliant",
    "PCI certified",
    "SOC 2 compliant",
    "production-ready",
    "fully secure",
    "one Bedrock call",
    "all traces are PII-scrubbed",
    "tested against a real credit bureau",
    "a real payment processor",
    "the payment-allocation placement is still an open client decision",
    "late-fee compounding is settled in the code",
    "fairness has been evaluated",
    "the E2E suite is green in parallel",
    "the client has not decided fairness policy",
    "the synthetic package is vendor-issued documentation",
)

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


def _live_files() -> list[pathlib.Path]:
    files: list[pathlib.Path] = []
    for root in SURFACES:
        for path in root.rglob("*.tsx"):
            if "node_modules" in path.parts or ".next" in path.parts:
                continue
            files.append(path)
    return files


def test_the_list_does_not_forbid_required_copy():
    """"Captured -- allocation pending" is the copy the payment receipt is
    REQUIRED to show. A forbidden-claims list that swept it in would fail the
    component that gets it right."""
    assert not any("allocation pending" in c.lower() for c in FORBIDDEN_CLAIMS)


def test_normalisation_sees_through_markdown_and_jsx():
    """FC-03, pinned directly rather than trusted.

    A phrase carrying `**emphasis**` and a page authoring the same words plainly
    must compare equal, and so must a sentence JSX has split.
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
    phrases = FORBIDDEN_CLAIMS

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
        "these live surfaces carry a claim we must not make, with no 'inherited vendor claims -- not verified by Meridian' "
        "qualifier beside it:\n"
        + "\n".join(sorted(set(offences)))
        + "\n\nEither remove the claim or render it beside the qualifier "
        "(docs/DEBT.md D25)."
    )
