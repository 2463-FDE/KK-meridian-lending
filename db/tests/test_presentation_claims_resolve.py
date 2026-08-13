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
    # An unrecognised status must not slip past as "not merged, nothing to do".
    # test_every_manifest_status_is_recognised owns that failure; this asserts
    # the vocabulary here too, so tightening one check cannot quietly loosen
    # this one.
    assert row.get("status") in MANIFEST_STATUSES, (
        f"PR #{pr} has an unrecognised status {row.get('status')!r}"
    )
    if row["status"] != "merged":
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
    assert row.get("status") in MANIFEST_STATUSES, (
        f"PR #{pr} has an unrecognised status {row.get('status')!r}"
    )
    if row["status"] != "open":
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

# --- manifest status validation ----------------------------------------------
#
# The two checks above each return early when the status is not the one they
# handle. That is fine for the two statuses that exist and silently wrong for
# any other: a row typed `landed`, `merge` or `closed` matched neither branch,
# so it was never checked for an artifact AND never required an open label. The
# manifest could carry a status nothing understood and the suite stayed green --
# an unrecognised value read as "checked" when it meant "skipped".
#
# So the vocabulary is closed. Exactly two statuses are legal, and anything else
# is an explicit failure rather than a quiet pass.

MANIFEST_STATUSES = ("merged", "open")


def _status_problem(pr, row):
    """Return a human-readable problem with this row, or None if it is sound.

    Pure and row-shaped on purpose: a synthetic row can be passed straight in,
    so the rejection of an unsupported status is provable without writing a
    broken manifest to disk and hoping the parser sees it the same way.
    """
    status = (row or {}).get("status")
    if status not in MANIFEST_STATUSES:
        return (
            f"PR #{pr} has status {status!r}, which is not one of "
            f"{list(MANIFEST_STATUSES)}. An unrecognised status is skipped by "
            f"every other check, so the row would be accepted on no evidence."
        )
    if status == "merged" and not (row or {}).get("artifact"):
        return (
            f"PR #{pr} is listed merged with no durable artifact. A merged row "
            f"is the one thing a reader can verify offline; without a file it "
            f"is a bare PR number again."
        )
    if status == "open" and (row or {}).get("artifact"):
        return (
            f"PR #{pr} is open but names artifact {row['artifact']!r}. Open "
            f"means nothing has landed, so a row that points at a file invites "
            f"the reader to believe proposed work has repository evidence -- "
            f"the same overclaim in a new column."
        )
    return None


@pytest.mark.parametrize("pr", sorted(_manifest_rows()))
def test_every_manifest_status_is_recognised(pr):
    problem = _status_problem(pr, _manifest_rows()[pr])
    assert problem is None, problem


@pytest.mark.parametrize("status", ["landed", "merge", "closed", "verified",
                                    "specified", "draft", "", "MERGED?"])
def test_an_unsupported_status_is_rejected(status):
    """The regression proof for the finding.

    Each of these previously produced a green run: neither the artifact check
    nor the open-label check claimed the row, so nothing looked at it. `landed`
    and `merge` are the realistic typos -- both are words this deck legitimately
    uses elsewhere, which is exactly why one would be easy to write here.
    """
    problem = _status_problem(99, {"status": status, "artifact": "docs/DEBT.md"})
    assert problem is not None, (
        f"status {status!r} was accepted; an unrecognised status is skipped by "
        f"every other check, so the row passes on no evidence"
    )
    assert "not one of" in problem


def test_a_merged_row_without_an_artifact_is_rejected():
    problem = _status_problem(99, {"status": "merged", "artifact": None})
    assert problem is not None and "no durable artifact" in problem


def test_a_well_formed_row_is_accepted():
    """Guard the guard: if _status_problem returned a string unconditionally,
    every rejection test above would pass while the manifest was unusable."""
    assert _status_problem(99, {"status": "merged", "artifact": "docs/DEBT.md"}) is None
    assert _status_problem(99, {"status": "open", "artifact": None}) is None


def test_the_manifest_actually_uses_both_statuses():
    """Both branches of the vocabulary are exercised by real rows.

    If every row were `merged`, the open-label check would never run against
    anything and would be dead code reported as coverage.
    """
    statuses = {r["status"] for r in _manifest_rows().values()}
    assert statuses == set(MANIFEST_STATUSES), (
        f"the manifest uses {sorted(statuses)}; both {list(MANIFEST_STATUSES)} "
        f"should appear or one of the checks is never exercised"
    )


def test_an_open_row_may_not_name_an_artifact():
    """The other half of the split the deck now describes.

    `merged` requires a file; `open` requires the absence of one. Only the first
    was enforced, so an open row could have pointed at a path that happened to
    exist and read as landed evidence -- which is precisely the confusion the
    open label exists to prevent.
    """
    problem = _status_problem(28, {"status": "open", "artifact": "docs/DEBT.md"})
    assert problem is not None and "nothing has landed" in problem


def test_the_deck_does_not_claim_every_pr_maps_to_a_file():
    """The wording finding, pinned.

    The evidence section said the manifest "maps each one to a file that exists
    in this repository" while carrying an open row with no artifact. A reader
    could come away believing the open maker-checker specification had durable
    repository evidence.
    """
    text = DECK.read_text(encoding="utf-8")
    assert "maps each one to a file that exists" not in text, (
        "the deck claims every cited PR resolves to an existing file, which is "
        "false for any open row"
    )
    assert "no artifact at all" in text, (
        "the deck does not state that an open PR has no landed artifact"
    )


def test_the_manifest_prose_does_not_overclaim_either():
    """The manifest's own introduction is a claim on the same page as the table.

    Correcting the deck left this behind: the manifest still said the resolver
    "asserts every PR reference in the deck appears here with a resolvable
    artifact", directly above a table whose #28 row has none. A reader trusting
    the prose over the table would believe the open specification had landed
    evidence -- which is the whole failure this file was written to stop, made
    by the file itself.
    """
    text = MANIFEST.read_text(encoding="utf-8")
    assert "with a resolvable artifact" not in text, (
        "the manifest claims every cited PR has a resolvable artifact, which is "
        "false for any open row"
    )
    assert "names **no artifact**" in text, (
        "the manifest does not state that an open row has no artifact"
    )
