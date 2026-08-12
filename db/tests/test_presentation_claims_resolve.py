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
import subprocess

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


# --- PR-backed claims ---------------------------------------------------------
#
# The resolver above only validated backticked repository paths, so `PR #22`,
# `PR #23` and `PR #24` passed through unchecked. A wrong number, a deleted PR
# or one that quietly reopened would still have gone green -- and those PRs are
# what back the internal-token and Decimal-boundary claims on the slides.
#
# A pull request is not durable evidence: it can be renumbered or reopened, and
# a reader offline cannot check it at all. So each one is resolved through
# docs/presentations/evidence-manifest.md to something that survives the PR.

MANIFEST = DOCS / "evidence-manifest.md" if (DOCS := DECK.parent) else None


def _manifest_rows():
    rows = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\|\s*#(\d+)\s*\|\s*(\w+)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$", line)
        if m:
            number, status, artifact, commit = m.groups()
            path = re.search(r"`([^`]+)`", artifact)
            rows[int(number)] = {
                "status": status.lower(),
                "artifact": path.group(1) if path else None,
                "commit": (re.search(r"`([0-9a-f]{7,40})`", commit) or [None, None])[1],
            }
    return rows


def _cited_prs():
    return sorted(set(int(n) for n in re.findall(r"PR #(\d+)", DECK.read_text(encoding="utf-8"))))


def test_the_manifest_exists():
    assert MANIFEST.is_file(), (
        "the deck cites pull requests but there is no evidence manifest to "
        "resolve them against"
    )


@pytest.mark.parametrize("pr", _cited_prs())
def test_every_cited_pr_is_in_the_manifest(pr):
    assert pr in _manifest_rows(), (
        f"the deck cites PR #{pr}, which is not in evidence-manifest.md. An "
        f"audience cannot verify a bare PR number, and it stops resolving the "
        f"moment the PR is archived or renumbered."
    )


@pytest.mark.parametrize("pr", _cited_prs())
def test_a_merged_pr_names_an_artifact_that_exists(pr):
    row = _manifest_rows().get(pr) or {}
    if row.get("status") != "merged":
        return
    artifact = row.get("artifact")
    assert artifact, f"PR #{pr} is listed merged with no durable artifact"
    assert (REPO / artifact).exists(), (
        f"PR #{pr} cites {artifact}, which does not exist on this branch -- so "
        f"the claim it backs cannot be checked by anyone reading the deck."
    )


@pytest.mark.parametrize("pr", _cited_prs())
def test_an_open_pr_is_labelled_open_in_the_deck(pr):
    """An open PR has no landed artifact, so the deck must say so."""
    row = _manifest_rows().get(pr) or {}
    if row.get("status") != "open":
        return
    context = " ".join(_line_context(f"PR #{pr}"))
    assert any(m in context for m in OPEN_MARKERS), (
        f"PR #{pr} has not landed, but the deck presents its claim without an "
        f"open marker. That is the overclaim this test exists to catch."
    )


@pytest.mark.parametrize("pr", _cited_prs())
def test_a_recorded_merge_commit_resolves(pr):
    """Checked when the object is present.

    CI checks out with limited history, so this assertion is skipped -- with a
    reason -- on a shallow clone rather than passing silently. The artifact
    assertion above never skips, so no row is ever accepted on no evidence.
    """
    row = _manifest_rows().get(pr) or {}
    commit = row.get("commit")
    if not commit:
        return
    if subprocess.run(["git", "rev-parse", "--is-shallow-repository"],
                      cwd=REPO, capture_output=True, text=True).stdout.strip() == "true":
        pytest.skip("shallow clone -- merge commits are not in this checkout")
    found = subprocess.run(["git", "cat-file", "-e", f"{commit}^{{commit}}"],
                           cwd=REPO, capture_output=True)
    assert found.returncode == 0, (
        f"PR #{pr} records merge commit {commit}, which is not in this repository"
    )


def test_the_pr_checks_are_not_vacuous():
    """A parametrized test over an empty list passes and proves nothing."""
    assert len(_cited_prs()) >= 3, f"found almost no PR citations: {_cited_prs()}"
    merged = [p for p, r in _manifest_rows().items() if r["status"] == "merged"]
    assert merged, "the manifest lists no merged PR, so the artifact check never ran"
