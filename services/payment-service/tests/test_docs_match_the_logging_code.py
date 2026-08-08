"""Documentation must not claim a PAN/CVV/SSN log that the code does not have.

This repository has now produced the same false finding three times, each time
from a document rather than from code:

  * four `logging_config.py` docstrings claimed a request-body middleware that
    has never existed (D5c);
  * `docs/DEBT.md` D5a recorded servicing and origination as open on the
    strength of those docstrings;
  * `docs/runbook.md` and `docs/ROADMAP.md` still asserted that
    `payment-service` logs full PAN/CVV/SSN at INFO, that servicing formats
    them into a log line, and that origination's request middleware logs whole
    POST bodies unredacted.

Every one of those was contradicted by the code at the time it was read. The
cost is not cosmetic: a comment that OVERSTATES a defect produces false
findings as reliably as one that understates it, and each round cost a reader
the time to disprove it.

So this test asserts the two halves together:

  1. the CODE property -- no logging call site in this service can emit a raw
     PAN, CVV or SSN, because the request schema forbids those fields entirely;
  2. the DOCUMENTATION property -- no shipped document makes the present-tense
     claim that it does.

The second half is deliberately narrow. It matches a short list of specific
sentences that were actually wrong, not the words "PAN" or "CVV" in general --
those appear legitimately throughout the docs when describing history, the
handover state, or what is still stored. A test that failed on every mention
would be noise, and noise is how the next stale claim gets waved through.
"""
import pathlib
import re

import pytest

from app import schemas

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

DOCS = [
    REPO_ROOT / "docs" / "runbook.md",
    REPO_ROOT / "docs" / "ROADMAP.md",
    REPO_ROOT / "docs" / "DEBT.md",
    REPO_ROOT / "ARCHITECTURE.md",
]

# Present-tense claims that the code disproves. Each pattern is one sentence
# that was actually committed to this repository and was actually false.
FALSE_CLAIMS = [
    (
        re.compile(r"payment-service`?\s+logs\s+full\s+PAN", re.IGNORECASE),
        "payment-service cannot receive a raw PAN: PaymentIn forbids the field",
    ),
    (
        re.compile(r"still\s+formats\s+PAN/CVV/SSN\s+into\s+a\s+log", re.IGNORECASE),
        "servicing's charge logs loan_id/amount/method only",
    ),
    (
        re.compile(r"request\s+middleware\s+still\s+logs\s+full\s+POST\s+bodies", re.IGNORECASE),
        "no service has request-body middleware; it has never existed (D5c)",
    ),
    (
        re.compile(r"still\s+logs\s+PAN/CVV/SSN\s+in\s+the\s+clear", re.IGNORECASE),
        "no service logs PAN/CVV/SSN; ADR 0008 removed the fields from the wire",
    ),
    (
        re.compile(r"origination\s+still\s+logs\s+full\s+PII", re.IGNORECASE),
        "origination's intake logs app_id/applicant_id only (PR #6 review, Gap C)",
    ),
]


# A document is allowed to QUOTE a claim it is retracting -- recording what a
# page used to assert, and why it was wrong, is how the next reader avoids
# re-deriving it. So a match on a line that also carries a retraction cue is not
# a live claim.
#
# This is a deliberately narrow escape hatch: the cue has to say that the
# statement is historical or false. It is not "add a word to silence the test" --
# a line claiming a PAN is logged today cannot honestly carry any of these.
# Found by this test failing on its own fix, which is the behaviour it should
# have.
RETRACTION_CUES = re.compile(
    r"previously said|used to (say|claim)|was false|are false|is false|never existed"
    r"|no longer|this entry previously|described the state|stale",
    re.IGNORECASE,
)


def _docs():
    return [(d, d.read_text(encoding="utf-8")) for d in DOCS if d.is_file()]


def test_the_documents_this_test_guards_are_present():
    """A path typo would make every assertion below vacuous."""
    found = {d.name for d, _ in _docs()}
    missing = {d.name for d in DOCS} - found
    assert not missing, f"guarded documents not found: {sorted(missing)}"


@pytest.mark.parametrize("pattern,why", FALSE_CLAIMS, ids=[c[1][:40] for c in FALSE_CLAIMS])
def test_no_document_repeats_a_disproved_logging_claim(pattern, why):
    offenders = []
    for path, text in _docs():
        lines = text.splitlines()
        for n, line in enumerate(lines, 1):
            if not pattern.search(line):
                continue
            # SAME LINE only. A first version looked at neighbouring lines too,
            # on the theory that a wrapped retraction might put the cue next
            # door -- and a mutation test then showed the hole: appending a
            # fresh, live false claim three lines below an existing retraction
            # was silently excused. The escape hatch has to be narrower than
            # the thing it excuses, so the cue must sit in the same line as the
            # claim it retracts. If that makes a document awkward to word, the
            # document should be reworded, not this test.
            if RETRACTION_CUES.search(line):
                continue
            offenders.append(f"{path.relative_to(REPO_ROOT)}:{n}")
    assert not offenders, (
        f"{offenders} repeat a claim the code disproves -- {why}. If the code has "
        f"regressed, fix the code; if not, fix the document. Do not silence this "
        f"test by rewording the sentence."
    )


def test_the_request_schema_makes_a_raw_pan_unreachable():
    """The code half of the same claim.

    The documentation assertions above are only meaningful while this holds: if
    the schema ever accepted `pan` again, the docs would be right and this test
    would be the thing that is wrong. Asserted here so the two move together.
    """
    fields = set(schemas.PaymentIn.model_fields)
    for forbidden in ("pan", "cvv", "ssn"):
        assert forbidden not in fields, (
            f"PaymentIn accepts {forbidden!r} again -- the documents this test "
            f"guards would now be telling the truth"
        )
    # extra="forbid": a client still sending pan/cvv/ssn gets a 422 rather than
    # a silent drop, so the field cannot arrive and be logged by accident.
    assert schemas.PaymentIn.model_config.get("extra") == "forbid"


def test_a_rejected_payload_carrying_a_pan_never_becomes_a_log_line():
    """End of the same argument, exercised rather than reasoned about."""
    with pytest.raises(Exception) as exc:
        schemas.PaymentIn(
            loan_id=1, processor_token="tok_test_placeholder_value", last4="1111",
            brand="visa", amount=10.0, idempotency_key="k",
            pan="4111111111111111", cvv="123",
        )
    # Pydantic names the offending fields, which is what makes the rejection
    # auditable rather than merely effective.
    message = str(exc.value)
    assert "pan" in message and "cvv" in message
