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

# The docstrings that started this, guarded as first-class documentation. D5c
# was a *source* defect: four `logging_config.py` docstrings asserted a
# request-body middleware, and two separate readers reported it as a live PII
# gap. Guarding only the Markdown would leave that primary failure mode -- the
# claim returning to the file it came from -- completely unprotected.
SOURCES = [
    REPO_ROOT / "services" / svc / "app" / "logging_config.py"
    for svc in (
        "origination-service",
        "decision-service",
        "kyc-service",
        "servicing-service",
        "payment-service",
        "disclosure-service",
    )
]

GUARDED = DOCS + SOURCES

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
    (
        # The original D5c docstring, verbatim in its load-bearing half. This is
        # the sentence that produced two false findings; it is guarded in the
        # source files as well as the Markdown.
        re.compile(r"logs\s+the\s+full\s+request\s+body", re.IGNORECASE),
        "no service has request-body middleware; logging_config wires handlers only (D5c)",
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
    r"previously said|previously claimed|previously recorded|used to (say|claim)"
    r"|(docstring|comment|entry|row|cell|document|documents|roadmap|runbook|README)s?\s+claimed"
    r"|was false|are false|is false|which is false|never existed|has never existed"
    r"|no longer|this entry previously|described the state|stale",
    re.IGNORECASE,
)

# A retraction excuses the CLAUSE it sits in, not the whole physical line.
#
# Markdown table rows are one physical line carrying five or six independent
# sentences, so a line-wide cue check let a live false claim ride along beside
# an unrelated retraction elsewhere in the same row: appending
# "payment-service logs full PAN" to `docs/DEBT.md` D5a passed, because that
# row already contains the word "stale" for a different reason. Splitting on
# sentence ends and table-cell pipes is enough to separate them -- and the
# split deliberately requires whitespace after the terminator, so "7.99%" or
# "ADR 0008." mid-number stays in one piece. Closing emphasis markers are
# allowed between the terminator and the space: a sentence ending `...code.*`
# is still a sentence end, and missing that was how the first version of this
# fix still passed the mutation it was written against.
CLAUSE_BOUNDARY = re.compile(r"[.;][*_`\"')\]]*\s+|\|")


def _clause_around(line: str, start: int, end: int) -> str:
    """The sentence/cell of `line` that contains the span [start, end)."""
    cut_before = 0
    cut_after = len(line)
    for boundary in CLAUSE_BOUNDARY.finditer(line):
        if boundary.end() <= start:
            cut_before = boundary.end()
        elif boundary.start() >= end:
            cut_after = boundary.start()
            break
    return line[cut_before:cut_after]


def live_claim_lines(text: str, pattern: re.Pattern) -> list:
    """Line numbers where `pattern` matches with no retraction in its clause."""
    hits = []
    for n, line in enumerate(text.splitlines(), 1):
        for match in pattern.finditer(line):
            if RETRACTION_CUES.search(_clause_around(line, match.start(), match.end())):
                continue
            hits.append(n)
            break
    return hits


def _docs():
    return [(d, d.read_text(encoding="utf-8")) for d in GUARDED if d.is_file()]


def test_the_documents_this_test_guards_are_present():
    """A path typo would make every assertion below vacuous."""
    found = {d for d, _ in _docs()}
    missing = [str(d.relative_to(REPO_ROOT)) for d in GUARDED if d not in found]
    assert not missing, f"guarded documents not found: {sorted(missing)}"


def test_the_source_docstrings_that_started_d5c_are_guarded():
    """The Markdown was downstream of these files; guarding it alone is half a fix."""
    guarded = {d.resolve() for d in GUARDED}
    for source in SOURCES:
        assert source.is_file(), f"{source} is missing from the repository"
        assert source.resolve() in guarded, f"{source} is not in the guarded corpus"


@pytest.mark.parametrize("pattern,why", FALSE_CLAIMS, ids=[c[1][:40] for c in FALSE_CLAIMS])
def test_no_document_repeats_a_disproved_logging_claim(pattern, why):
    offenders = []
    for path, text in _docs():
        # SAME CLAUSE only. A first version looked at neighbouring lines, then
        # at the whole physical line; mutation testing killed both. Three lines
        # below an existing retraction was excused, and then anywhere in the
        # same table row was excused. The escape hatch has to be narrower than
        # the thing it excuses. If that makes a document awkward to word, the
        # document should be reworded, not this test.
        offenders += [
            f"{path.relative_to(REPO_ROOT)}:{n}" for n in live_claim_lines(text, pattern)
        ]
    assert not offenders, (
        f"{offenders} repeat a claim the code disproves -- {why}. If the code has "
        f"regressed, fix the code; if not, fix the document. Do not silence this "
        f"test by rewording the sentence."
    )


PAN_CLAIM = FALSE_CLAIMS[0][0]
REQUEST_BODY_CLAIM = FALSE_CLAIMS[-1][0]

# The exact row the escape hatch used to leak through, trimmed to its shape: a
# real `docs/DEBT.md` D5a-style table row whose retraction is about something
# else entirely and sits several sentences away from the appended claim.
_ROW_WITH_AN_UNRELATED_RETRACTION = (
    "| D5a | Card/PII written to logs at INFO. | **Fixed.** Per site: "
    "`origination/intake.py` -> `app_id`/`applicant_id`. *This row previously "
    "recorded servicing as still open -- that was read off a stale docstring "
    "rather than the code.* {claim} | `servicing-service/app/payments.py` |"
)


def test_a_live_claim_elsewhere_in_a_retracting_row_is_still_caught():
    """The mutation that proved the line-wide hatch was too wide.

    `stale` appears in this row for an unrelated reason. A cue scoped to the
    physical line therefore excused the appended sentence, which is a live,
    false, present-tense claim. Scoped to the clause, it does not.
    """
    mutated = _ROW_WITH_AN_UNRELATED_RETRACTION.format(
        claim="payment-service logs full PAN on every charge."
    )
    assert live_claim_lines(mutated, PAN_CLAIM) == [1]


def test_the_same_row_without_the_appended_claim_stays_clean():
    """The other half: the guard must not fail the row it is meant to allow."""
    assert live_claim_lines(_ROW_WITH_AN_UNRELATED_RETRACTION.format(claim=""), PAN_CLAIM) == []


def test_a_retraction_in_the_matched_sentence_still_excuses_it():
    """Quoting a claim in order to disprove it remains legal, as it must be."""
    honest = (
        "| D5a | notes | This entry previously said payment-service logs full PAN "
        "at INFO; that is false against the current code. | ref |"
    )
    assert live_claim_lines(honest, PAN_CLAIM) == []


def test_the_original_d5c_docstring_wording_is_caught_if_it_returns():
    """Returning the sentence to a source docstring must fail the guard."""
    regressed = (
        '"""Logging setup.\n\n'
        "Logs the full request body on every POST -- including PII. No redaction.\n"
        '"""\n'
    )
    assert live_claim_lines(regressed, REQUEST_BODY_CLAIM) == [3]


def test_the_shipped_source_docstrings_do_not_carry_that_wording():
    """...and they do not carry it today."""
    for source in SOURCES:
        assert live_claim_lines(source.read_text(encoding="utf-8"), REQUEST_BODY_CLAIM) == [], (
            f"{source.relative_to(REPO_ROOT)} claims a request-body middleware again"
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
