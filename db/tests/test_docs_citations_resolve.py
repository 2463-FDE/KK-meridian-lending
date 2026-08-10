"""Every file path a document cites must be a file, or say why it is not yet.

This repository has now produced the same class of finding several times: a
document asserting something the code does not say. Stale *claims* are guarded in
`payment-service/tests/test_docs_match_the_logging_code.py`; this guards stale
*citations*, which fail differently and just as expensively — a reader follows
`services/decision/graph.py`, finds nothing, and cannot tell whether the file
moved, the claim is wrong, or they mistyped it.

Three real examples this test was written from, all found by hand during PR #17:

  * `decision/graph.py`, `kyc/routers/kyc.py`, `origination/intake.py` — real
    files under shorthand paths that resolve nowhere;
  * ``specs/0001-...md`` — a citation truncated with an ellipsis, committed;
  * `db/bench/graph_traversal_benchmark.py` — a path that exists only on an
    unmerged branch.

The last one is why this is not simply "every path must exist". A roadmap
legitimately points at work in flight; what it must not do is present that path
as something the reader can open. So a citation may be unresolved **only if its
own line says so** — the same same-clause rule the logging guard uses, for the
same reason: a disclaimer three paragraphs away is not a disclaimer.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

DOCS = [
    REPO / "README.md",
    REPO / "ARCHITECTURE.md",
    REPO / "docs" / "ROADMAP.md",
    REPO / "docs" / "DEBT.md",
    REPO / "docs" / "runbook.md",
    REPO / "docs" / "RUNBOOK-pan-cvv-contract.md",
]
DOCS += sorted((REPO / "adr").glob("*.md"))
DOCS += sorted((REPO / "specs").glob("*.md"))

# A backticked path with a source-file extension, optionally with :line.
_CITATION = re.compile(
    r"`([A-Za-z0-9_./-]+\.(?:py|sql|ts|tsx|md|yml|yaml|json|txt))(?::\d+(?:-\d+)?)?`"
)

# Phrases that make an unresolved citation honest: the line itself says the path
# is not here yet. Kept short and specific -- a broad list would let any nearby
# hedge excuse a broken reference.
_FORWARD_LOOKING = re.compile(
    r"does not exist on `main`|arrives with|only on that branch|not on `main` yet"
    r"|will be added|on that PR's branch",
    re.IGNORECASE,
)


def _resolves(ref: str) -> bool:
    """Whether a cited path names a real file.

    Service-relative shorthand is accepted (`payment-service/app/main.py` for
    `services/payment-service/app/main.py`) because the docs use it consistently
    and it is unambiguous -- but a path that matches nothing under either root is
    a broken citation regardless of how readable it looks.
    """
    if (REPO / ref).exists():
        return True
    if list(REPO.glob("services/" + ref)):
        return True
    if list(REPO.glob("services/*/" + ref)):
        return True
    return False


def _citations():
    for doc in DOCS:
        if not doc.is_file():
            continue
        for lineno, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            for match in _CITATION.finditer(line):
                yield doc, lineno, line, match.group(1)


def test_the_citation_scan_finds_something_to_check():
    """A regex that matched nothing would make the test below vacuous."""
    found = list(_citations())
    assert len(found) > 50, (
        f"only {len(found)} citations found across {len(DOCS)} documents; the "
        f"pattern or the document list is broken"
    )


def test_every_cited_path_resolves_or_says_it_does_not_yet():
    broken = []
    for doc, lineno, line, ref in _citations():
        if "/" not in ref:
            continue                      # a bare filename is prose, not a path
        if _resolves(ref):
            continue
        if _FORWARD_LOOKING.search(line):
            continue                      # explicitly not here yet, on its own line
        broken.append(f"{doc.relative_to(REPO).as_posix()}:{lineno} -> {ref}")

    assert not broken, (
        "documents cite paths that do not resolve:\n  " + "\n  ".join(broken)
        + "\n\nEither correct the path, or -- if it genuinely arrives with an "
          "unmerged branch -- say so on the same line ('arrives with', 'does not "
          "exist on `main`'), so a reader is not sent looking for a file that is "
          "not here."
    )


def test_a_truncated_citation_is_not_allowed():
    """`specs/0001-...md` was committed and read as a real path.

    An ellipsis inside a citation is never correct: it looks like a filename and
    cannot be opened. Checked separately from resolution because the failure mode
    is different -- a reader does not know whether they are missing a file or the
    author was.
    """
    offenders = [
        f"{doc.relative_to(REPO).as_posix()}:{lineno} -> {ref}"
        for doc, lineno, _, ref in _citations()
        if "..." in ref
    ]
    assert not offenders, "truncated citations:\n  " + "\n  ".join(offenders)


def test_no_document_claims_a_merged_pull_request_is_still_open():
    """The specific staleness this PR exists to remove.

    PRs #8, #10, #11, #12, #13, #14, #15 and #16 are merged. A document saying one of them
    is unmerged, awaiting CI, or that its files live only on a branch is telling a
    reader to distrust `main` for no reason -- and that is how a correct document
    trains people to check nothing.

    Deliberately narrow: it matches merged PR numbers next to open-state wording,
    not the words "open" or "PR" in general. Historical notes are allowed to say
    what a row USED to claim, so a retraction cue on the same line exempts it --
    the same rule the logging guard uses.
    """
    merged = ("#8", "#10", "#11", "#12", "#13", "#14", "#15", "#16")
    open_state = re.compile(
        r"\b(?:not on `main`|only on that branch|still open|not (?:yet )?merged"
        r"|awaiting (?:CI|merge)|CI not green|no CI run exists)\b",
        re.IGNORECASE,
    )
    retraction = re.compile(
        r"previously|used to|was accurate|no longer|formerly|this (?:row|line|"
        r"paragraph|table) (?:used to|previously)|kept because|what this table"
        # A sentence that reports the old wording and dates it. Added for two
        # real cases: a status-legend entry recording the label it replaced
        # ('the label read "Still open for PR #8" while that was true'), and a
        # paragraph explaining that it had described the state while the PR was
        # open. Both are the honest form -- naming what changed and when -- and a
        # test that forbade them would push authors to delete the history
        # instead, which is the opposite of what this file is for.
        r"|while (?:that|it) was (?:true|open|still)"
        r"|label read|described the state while|was still open and",
        re.IGNORECASE,
    )

    offenders = []
    for doc in DOCS:
        if not doc.is_file():
            continue
        for lineno, line in enumerate(doc.read_text(encoding="utf-8").splitlines(), 1):
            if not open_state.search(line):
                continue
            if retraction.search(line):
                continue
            for pr in merged:
                if re.search(r"PR\s*" + re.escape(pr) + r"\b", line) or re.search(
                    r"\|\s*" + re.escape(pr) + r"\s*\|", line
                ):
                    offenders.append(
                        f"{doc.relative_to(REPO).as_posix()}:{lineno} says {pr} is "
                        f"unmerged: {line.strip()[:120]}"
                    )
                    break

    assert not offenders, (
        "documents describe merged pull requests as open:\n  "
        + "\n  ".join(offenders)
    )
