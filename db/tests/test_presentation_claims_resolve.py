"""Every path a slide cites must resolve, or be labelled as not yet landed.

A deck is read once, out loud, by someone who cannot check a path mid-sentence.
So the rule is stricter than for prose: a cited file either exists on this branch,
or the slide says explicitly that it is a draft on an open PR.

The failure this prevents is specific and was live in the first version: slide 1
cited `specs/0002` as though the specification were part of the system. It is a
draft on PR #28. Presenting proposed work as landed is the same defect as a policy
publishing a rule nothing implements -- an audience cannot tell the difference,
and here the audience is the client.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
DECK = REPO / "docs" / "presentations" / "2026-08-12-three-slides.md"

pytestmark = pytest.mark.skipif(not DECK.is_file(), reason="deck not on this branch")

#: Words that mark a citation as proposed rather than landed.
OPEN_MARKERS = ("not yet on `main`", "open — pr", "open -- pr", "draft in pr",
                "**open**", "not started", "not evidenced", "open gap")


def _cited_paths():
    text = DECK.read_text(encoding="utf-8")
    return sorted(set(re.findall(r"`((?:db|docs|services|specs|frontend)/[\w./\-]+)`", text)))


def _line_context(needle):
    text = DECK.read_text(encoding="utf-8").splitlines()
    return [l.lower() for l in text if needle in l]


@pytest.mark.parametrize("path", _cited_paths())
def test_every_cited_path_resolves_or_is_labelled_open(path):
    if (REPO / path).exists():
        return
    context = " ".join(_line_context(path))
    assert any(m in context for m in OPEN_MARKERS), (
        f"the deck cites {path}, which does not exist on this branch and is not "
        f"labelled as a draft on an open PR. An audience cannot tell proposed "
        f"work from landed work, and here the audience is the client."
    )


def test_the_deck_cites_the_debt_register_by_its_real_path():
    """`DEBT.md` does not exist at the repository root; `docs/DEBT.md` does. A
    citation that does not resolve is a broken claim, and this one is quoted as
    the tracking record for an open control gap."""
    text = DECK.read_text(encoding="utf-8")
    assert "docs/DEBT.md" in text
    bare = re.findall(r"(?<!/)(?<!docs/)`DEBT\.md`", text)
    assert not bare, f"the deck cites a bare DEBT.md {len(bare)} time(s)"


def test_it_found_paths_to_check():
    assert len(_cited_paths()) >= 8, "the deck cites almost nothing -- check the regex"


def test_the_oracle_is_not_described_as_an_external_authority():
    """The golden file is a checked-in artifact this team produced. Calling it a
    third party overstates it -- nobody outside the repository certified it."""
    text = DECK.read_text(encoding="utf-8").lower()
    assert "independent third party" not in text, (
        "the deck calls the golden vector file an independent third party"
    )
    assert "oracle artifact" in text, (
        "the deck does not say what the golden file actually is"
    )


def test_the_servicing_float_boundary_is_on_the_slide_not_only_in_the_notes():
    """A limitation that lives only in speaker notes is not disclosed to anyone
    reading the deck afterwards."""
    text = DECK.read_text(encoding="utf-8")
    slide3 = text[text.index("## Slide 3"):]
    on_screen = slide3[:slide3.index("### Notes")]
    assert "float" in on_screen.lower(), (
        "the servicing float boundary appears only in the notes, so it is absent "
        "from what the room and any later reader actually see"
    )
