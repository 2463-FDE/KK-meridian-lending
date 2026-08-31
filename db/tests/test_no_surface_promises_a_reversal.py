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

import pytest

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
    # The maintained TESTS that describe the same workflow. Codex review
    # REV-COPY-02: both of these still taught that "a reversal goes through the
    # maker-checker" after the page copy and the service docstring had been
    # corrected. A test docstring is documentation the next implementer reads --
    # arguably the documentation they trust most, because it sits beside an
    # assertion that passes.
    SERVICES / "servicing-service" / "tests" / "test_review_queue_api.py",
    SERVICES / "servicing-service" / "tests" / "test_review_queue_real_postgres.py",
)

#: Words for the capability Meridian does not have.
_REVERSAL_WORD = re.compile(r"\b(revers\w+|refund\w*|chargeback\w*|void(?:ing|ed)?)\b",
                            re.IGNORECASE)

#: The mechanism those words must not be attached to as a capability.
_MECHANISM = re.compile(r"maker[- ]?checker|approvals\b", re.IGNORECASE)

#: What makes a CLAUSE a denial or a historical note rather than an instruction.
#:
#: Codex review REV-GUARD-03. The first version of this evaluated whole SENTENCES
#: and treated any negation anywhere in one as a denial -- so the original toast,
#:
#:     "No money moved -- a reversal goes through Approvals."
#:
#: passed clean: the leading "No" negates the MONEY MOVEMENT, not the reversal, and
#: the second clause still promised one. The guard that justifies this whole PR did
#: not hold against the defect the PR was written for. Reproduced before fixing:
#: `_teaches_a_reversal` returned `[]` on that exact string.
#:
#: So negation now has to be LOCAL to the clause that names the reversal. A denial
#: is a denial of the reversal, not of something else in the same sentence.
_NEGATED_OR_HISTORICAL = re.compile(
    r"\bno\s+(?:card\s+)?(?:refund|revers\w+|chargeback|void\w*)"
    r"|\bnot\s+a\s+(?:card\s+)?(?:refund|revers\w+|chargeback)"
    r"|\bno\s+(?:such\s+)?(?:reversal|refund)\s+capability"
    r"|\b(?:cannot|can[' ]?not|can[' ]?t|does\s+not|do\s+not|doesn[' ]?t|never)\b"
    r"|\bhas\s+no\b|\bis\s+not\b|\bare\s+not\b|\bwithout\b|\bnothing\b"
    r"|\bused\s+to\b|\bpreviously\b|\bformerly\b|\bfalse\b|\bincorrect\b"
    r"|\bREV-COPY\b|\bREV-GUARD\b",
    re.IGNORECASE,
)

#: Clause boundaries. A dash, semicolon or colon separates two independent claims,
#: and the toast above is exactly that shape: a true first clause and a false
#: second one joined by an em dash.
_CLAUSE_SPLIT = re.compile(r"\s+--\s+|\s*[;:]\s*|\s*\u2014\s*|\s+-\s+")


def _sentences(text: str):
    """Rough sentence split. Good enough, and deliberately not a parser."""
    for block in re.split(r"\n\s*\n", text):
        flat = " ".join(block.split())
        for sentence in re.split(r"(?<=[.!?])\s+", flat):
            if sentence.strip():
                yield sentence.strip()


def _teaches_a_reversal(text: str) -> list[str]:
    """Clauses that attach a reversal capability to a real mechanism, unnegated.

    The invariant, stated once: *no maintained operator, runtime or test
    description may teach that maker-checker (or Approvals) reverses a payment.*

    Evaluated per CLAUSE rather than per sentence, because a sentence can be half
    true: "No money moved -- a reversal goes through Approvals" negates the money
    movement and still promises the reversal. Splitting on dash, semicolon and
    colon puts each claim on its own, so the denial has to be a denial OF THE
    REVERSAL to clear it.
    """
    out = []
    for sentence in _sentences(text):
        for clause in _CLAUSE_SPLIT.split(sentence):
            clause = clause.strip()
            if not clause:
                continue
            if not _REVERSAL_WORD.search(clause):
                continue
            if not _MECHANISM.search(clause):
                continue
            if _NEGATED_OR_HISTORICAL.search(clause):
                continue
            out.append(clause[:160])
    return out


# --------------------------------------------------------------------------
# The guard's own behaviour, pinned against the sentences it exists to catch.
#
# Codex review REV-GUARD-03: the first semantic version of this guard did NOT
# flag the original toast, because it evaluated whole sentences and read the
# leading "No money moved" as a denial of the reversal. A guard that misses the
# defect its own PR removed is worse than no guard, so the shapes are now test
# data rather than something a reader has to trust.
# --------------------------------------------------------------------------

#: Every wording this PR removed, verbatim. Each MUST be flagged.
_MUST_FLAG = [
    # The success toast. Two clauses: the first true, the second false.
    "No money moved -- a reversal goes through Approvals.",
    # Same, with the em dash the page actually rendered.
    "No money moved — a reversal goes through Approvals.",
    # The paragraph under the disposition buttons.
    "Recording an answer moves no money. A reversal is a separate, two-person "
    "decision in Approvals.",
    # `test_review_queue_api.py`, before this PR.
    "a reversal still goes through the maker-checker, with the second person "
    "that requires",
    # `test_review_queue_real_postgres.py`, before this PR.
    "Reversing the payment goes through the maker-checker, with the second "
    "person that requires.",
    # The third passage, found unprompted.
    "A proposal queued automatically would put a reversal one click from "
    "happening in Approvals.",
    # `review_queue.py`, before this PR.
    "A reviewer who concludes confirmed_duplicate still has to go through the "
    "maker-checker to reverse anything.",
    # A sentence whose FIRST clause denies a reversal and whose second still
    # promises one. This is the case only clause-splitting catches: at sentence
    # level the leading "no card reversal" reads as a denial and clears the whole
    # thing, which is REV-GUARD-03 in its most direct form.
    "There is no card reversal here -- a reversal goes through Approvals.",
]

#: Wording that is TRUE and must stay writable. Each MUST NOT be flagged --
#: otherwise the guard forbids describing the defect and gets gamed instead of
#: fixed.
_MUST_NOT_FLAG = [
    # The current page copy.
    "A correction is a separate two-person decision: a balance adjustment raised "
    "on the loan's account page and approved by a different person in Approvals. "
    "There is no card refund or reversal in this system.",
    # The current toast.
    "No money moved. A correction is a balance adjustment approved by a different "
    "person in Approvals; there is no card reversal.",
    # The current service docstring's denial.
    "Maker-checker cannot do it: ENTRY_TYPES is {adjustment, fee_waived}, and no "
    "service in this repository exposes a refund, void, reversal or chargeback "
    "route.",
    # A historical note, which this repository writes deliberately.
    "This docstring used to send the reviewer through maker-checker to undo the "
    "payment.",
    # Reconciliation reading refund lines is not performing one.
    "Reconciliation parses refund lines out of the processor's settlement file.",
]


@pytest.mark.parametrize("text", _MUST_FLAG)
def test_the_guard_flags_every_wording_this_pr_removed(text):
    assert _teaches_a_reversal(text), (
        "the guard does not catch a sentence this PR removed, which is the "
        f"REV-GUARD-03 defect returning: {text!r}")


@pytest.mark.parametrize("text", _MUST_NOT_FLAG)
def test_the_guard_permits_denials_and_historical_notes(text):
    assert not _teaches_a_reversal(text), (
        "the guard flags a true statement, so it forbids documenting the defect "
        f"and will be gamed rather than fixed: {text!r}")


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
    """The same rule, one layer down, and stated semantically.

    Codex review REV-COPY-01 then REV-COPY-02: the false workflow survived first
    in `review_queue.py` and then in two maintained test files, each time because
    the guard was scanning a narrower set than the claim lived in.

    So this no longer matches fixed phrases. It looks for a sentence that attaches
    a REVERSAL WORD to a real MECHANISM (maker-checker, Approvals) without
    negating or historicising it -- which is the actual invariant:

        no maintained operator, runtime or test description may teach that
        maker-checker reverses a payment.

    A sentence may still name the false workflow when it is denying it ("this is
    not a card reversal") or recording that it was once claimed ("used to say").
    That is deliberate, and it is why the guard is sentence-scoped rather than a
    forbidden substring: a check that fired on any mention of the defect would make
    documenting the defect impossible, and would be gamed rather than fixed.
    """
    offenders = []
    for path in _REVIEW_WORKFLOW_MODULES:
        assert path.exists(), f"{path} moved; this guard is now scanning nothing"
        for sentence in _teaches_a_reversal(
                path.read_text(encoding="utf-8", errors="ignore")):
            offenders.append(f"{path.relative_to(REPO).as_posix()}: {sentence}")
    assert not offenders, (
        "a maintained description teaches that maker-checker reverses a payment, "
        "and no service implements one:\n  " + "\n  ".join(offenders)
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
