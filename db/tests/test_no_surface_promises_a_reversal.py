"""No screen may offer a reversal this system cannot perform.

`/reconciliation` told a reviewer, for months: *"Recording an answer moves no
money. A reversal is a separate, two-person decision in Approvals."* There is no
reversal. Maker-checker accepts exactly two entry types -- `adjustment` and
`fee_waived` -- and no service exposes a refund, void, reversal or chargeback
route. An operator who confirmed a duplicate payment and followed that sentence
arrived at Approvals with no control that did what they had been told to do.

That is the same defect class as the reconciliation statement PR #114 replaced and
the empty break list its review caught: a screen confidently pointing at something
that is not there. It sat on the duplicate-payment path, which is the one a client
is most likely to probe.

**This guard is two-sided on purpose.** Asserting only that the phrase is gone
would rot the moment somebody built a real reversal -- the copy would then be
*required* to mention it, and a test forbidding the word would be preserving a
falsehood in the other direction. So it derives the backend capability first and
makes the two agree:

  * no reversal capability exists  -> no surface may promise one;
  * a reversal capability appears  -> this test fails, and the failure says to go
    and update the copy rather than the test.
"""
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]
SERVICES = REPO / "services"
FRONTEND = REPO / "frontend"

#: A money-returning operation, as a ROUTE rather than as a word. Reconciliation
#: parses `refund` lines out of the processor's settlement file and
#: `reconciliation.py` discusses them at length -- reading a refund somebody else
#: performed is not performing one, so a word search would report the parser as
#: the capability.
_REVERSAL_ROUTE = re.compile(
    r"@(?:app|router)\.(?:post|put|patch|delete)\(\s*[\"'][^\"']*"
    r"(?:refund|revers|void|chargeback)",
    re.IGNORECASE,
)

#: Maker-checker's vocabulary. If a reversal is ever added as an entry type it
#: will appear here, and this guard should notice that too.
_MAKER_CHECKER = SERVICES / "servicing-service" / "app" / "maker_checker.py"


def _service_sources():
    for path in sorted(SERVICES.rglob("*.py")):
        if "__pycache__" in str(path) or "/tests/" in path.as_posix():
            continue
        yield path


def _frontend_surfaces():
    """Pages and components a person actually reads. Not tests, not e2e."""
    for root in (FRONTEND / "app", FRONTEND / "components"):
        for path in sorted(root.rglob("*.tsx")):
            if "node_modules" in str(path):
                continue
            yield path


#: The backend modules that DOCUMENT the duplicate-review workflow.
#:
#: Codex review of this PR (REV-COPY-01) caught the reason this list exists: the
#: page copy was corrected while `review_queue.py` still told the next reader that
#: a `confirmed_duplicate` "has to go through the maker-checker to reverse
#: anything". The screen and the service behind it then disagreed, and the guard
#: as first written could not see it -- so the defect survived one file behind the
#: thing it was written to fix.
#:
#: Scoped to the modules that describe what a reviewer does next, rather than to
#: all of `services/`, because `reconciliation.py` legitimately discusses refund
#: LINES in the processor's settlement file at length. Reading a refund somebody
#: else performed is not claiming this system performs one, and the phrase
#: patterns below are written so that distinction survives.
_REVIEW_WORKFLOW_MODULES = (
    SERVICES / "servicing-service" / "app" / "review_queue.py",
    SERVICES / "servicing-service" / "app" / "maker_checker.py",
)


def test_no_service_exposes_a_reversal_route():
    """The premise the copy rests on, established rather than assumed.

    If this fails, a reversal now exists -- which is good news, and it means
    `test_no_surface_promises_a_reversal_that_does_not_exist` below is now
    checking the wrong thing. Update the UI copy and this file together.
    """
    offenders = []
    for path in _service_sources():
        text = path.read_text(encoding="utf-8", errors="ignore")
        for match in _REVERSAL_ROUTE.finditer(text):
            line = text[: match.start()].count("\n") + 1
            offenders.append(f"{path.relative_to(REPO).as_posix()}:{line}")
    assert not offenders, (
        "a refund/reversal/void route now exists: "
        f"{offenders}. That is a capability change, so the reconciliation copy "
        "that currently says 'There is no card refund or reversal in this system' "
        "is now false and must be updated in the same change."
    )


def test_maker_checker_still_accepts_only_adjustment_and_fee_waived():
    """The two operations the copy may legitimately point an operator at."""
    text = _MAKER_CHECKER.read_text(encoding="utf-8")
    match = re.search(r"ENTRY_TYPES\s*=\s*frozenset\(\{([^}]*)\}\)", text)
    assert match, "ENTRY_TYPES is no longer a frozenset literal in maker_checker.py"
    types = {t.strip().strip("\"'") for t in match.group(1).split(",") if t.strip()}
    assert types == {"adjustment", "fee_waived"}, (
        f"maker-checker's entry types changed to {sorted(types)}. If a reversal "
        "was added, the reconciliation copy must stop saying there is none."
    )


def test_no_surface_promises_a_reversal_that_does_not_exist():
    """No page may tell a reader a reversal is available somewhere.

    The rule is not "the word must never appear". `/reconciliation` now says
    *"There is no card refund or reversal in this system"*, which contains the
    word and is exactly the sentence this guard exists to protect. So what is
    forbidden is the word in a phrase that OFFERS one -- routed through Approvals,
    described as a decision, or otherwise presented as a thing a person can go and
    do.
    """
    forbidden = [
        # The original sentence and the shapes it would come back as.
        re.compile(r"reversal\s+is\s+a\s+separate", re.IGNORECASE),
        re.compile(r"reversal\s+(?:goes|go)\s+through", re.IGNORECASE),
        re.compile(r"reversal\s+in\s+Approvals", re.IGNORECASE),
        re.compile(r"(?:request|submit|raise|start)\s+a\s+(?:reversal|refund)",
                   re.IGNORECASE),
        re.compile(r"(?:reversal|refund)\s+(?:is|are)\s+(?:available|supported)",
                   re.IGNORECASE),
        # The shape the backend docstring used: maker-checker presented as the
        # thing that performs a reversal.
        re.compile(r"maker-checker\s+to\s+reverse", re.IGNORECASE),
        re.compile(r"to\s+reverse\s+anything", re.IGNORECASE),
    ]
    offenders = []
    for path in _frontend_surfaces():
        text = path.read_text(encoding="utf-8", errors="ignore")
        # Comments explain the history deliberately; they are not what a user
        # reads. Strip them so the record of the old wording can stay in the file.
        without_comments = re.sub(r"\{/\*.*?\*/\}", " ", text, flags=re.DOTALL)
        without_comments = re.sub(r"^\s*//.*$", " ", without_comments, flags=re.MULTILINE)
        for pattern in forbidden:
            if pattern.search(without_comments):
                offenders.append(
                    f"{path.relative_to(REPO).as_posix()}: {pattern.pattern}")
    assert not offenders, (
        "a live surface offers a reversal, and no service implements one: "
        f"{offenders}"
    )


def test_no_review_workflow_module_documents_a_reversal_either():
    """The same rule, one layer down.

    Codex review of this PR (REV-COPY-01): the page copy was corrected while
    `review_queue.py` still told the next reader that a `confirmed_duplicate`
    "has to go through the maker-checker to reverse anything". The screen and the
    service behind it disagreed, and the first version of this guard scanned only
    `.tsx` surfaces, so it could not see it.

    A docstring is not a screen, and the distinction is worth keeping: this is not
    checking what a borrower is shown, it is checking that the module explaining
    the workflow does not teach the false one. That is how the defect would have
    come back -- not through the page, but through the next person implementing
    against the page's own backend.

    Comments are NOT stripped here, unlike the frontend scan, because in a Python
    module the docstring IS the documentation under test.
    """
    offenders = []
    for path in _REVIEW_WORKFLOW_MODULES:
        assert path.exists(), f"{path} moved; this guard is now scanning nothing"
        text = path.read_text(encoding="utf-8", errors="ignore")
        for pattern in (
            re.compile(r"maker-checker\s+to\s+reverse", re.IGNORECASE),
            re.compile(r"to\s+reverse\s+anything", re.IGNORECASE),
            re.compile(r"reversal\s+is\s+a\s+separate", re.IGNORECASE),
            re.compile(r"reversal\s+(?:goes|go)\s+through", re.IGNORECASE),
        ):
            if pattern.search(text):
                offenders.append(
                    f"{path.relative_to(REPO).as_posix()}: {pattern.pattern}")
    assert not offenders, (
        "a module documenting the review workflow still describes a reversal, "
        f"and no service implements one: {offenders}"
    )


def test_the_review_queue_module_names_the_supported_route_instead():
    """Having removed the false direction from the backend, it must give a true one."""
    text = (SERVICES / "servicing-service" / "app" / "review_queue.py").read_text(
        encoding="utf-8")
    assert re.search(r"balance ADJUSTMENT|balance adjustment", text), (
        "review_queue.py no longer names the operation that IS supported")
    assert re.search(r"ENTRY_TYPES` is\s*\n?`?\{adjustment, fee_waived\}|"
                     r"\{adjustment, fee_waived\}", text), (
        "review_queue.py no longer cites the two entry types that bound it")


def test_the_reconciliation_page_says_what_a_correction_actually_is():
    """Having removed the false direction, the page must still give a true one.

    Deleting the sentence outright would have left a reviewer who just confirmed a
    duplicate with no idea what to do next, which is a worse screen than the one
    with the wrong sentence. The replacement has to name the supported workflow.
    """
    page = (FRONTEND / "app" / "reconciliation" / "page.tsx").read_text(
        encoding="utf-8")
    assert "Recording an answer moves no money" in page, (
        "the true half of the original sentence was dropped")
    assert re.search(r"balance adjustment", page, re.IGNORECASE), (
        "the page no longer names the operation that IS supported")
    assert re.search(r"no card refund\s*\n?\s*or reversal", page, re.IGNORECASE), (
        "the page no longer says plainly that no reversal exists")
