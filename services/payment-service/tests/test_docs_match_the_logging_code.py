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
import os
import pathlib
import re

import pytest

from app import schemas
from app.redactor import redact_dict

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]

DOCS = [
    # README first: it is the document a reader meets before any of the others,
    # and it was the last one still carrying the claim.
    REPO_ROOT / "README.md",
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
] + [
    # The call site, not just the logging module. `applications.py` carried
    # `# creates applicant+application rows, logs full PII (D5 — KEEP)`
    # directly above the intake call -- a comment a reader trusts MORE than a
    # module docstring, because it sits on the line it describes, and one that
    # survived every earlier pass because the corpus was docstrings only.
    # Reviewed on PR #16.
    REPO_ROOT / "services" / "origination-service" / "app" / "routers" / "applications.py",
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
        # README said the seeds "still write card values into them", so "every
        # freshly initialised database contains card data". PR #11 stopped that:
        # both seed files insert last4/brand only. The columns still exist and
        # are still a PCI finding (D5b/D13) -- "the columns exist" and "there is
        # card data in them" are different claims, and conflating them made the
        # user-facing document overstate a real defect, which produces false
        # findings as reliably as understating one (see D5c). Guarded because it
        # is the sentence a reader meets first. Reviewed on PR #16.
        re.compile(
            r"seed\s+scripts\s+still\s+write\s+card\s+values"
            r"|freshly\s+initialised\s+database\s+contains\s+card\s+data",
            re.IGNORECASE,
        ),
        "db/init seeds insert last4/brand only; the pan/cvv columns are left NULL",
    ),
    (
        re.compile(r"origination\s+still\s+logs\s+full\s+PII", re.IGNORECASE),
        "origination's intake logs app_id/applicant_id only (PR #6 review, Gap C)",
    ),
    (
        # The call-site comment's own wording: `logs full PII (D5 — KEEP)`,
        # unqualified, sitting directly above intake.create_application(). The
        # pattern above needs the word "origination" and this comment does not
        # contain it -- it does not need to, because the file it lives in IS
        # origination. Matched on the bare claim, with the D5 citation optional
        # so a partial restoration fails too. Reviewed on PR #16.
        re.compile(r"logs\s+full\s+PII", re.IGNORECASE),
        "origination's intake logs app_id/applicant_id only (PR #6 review, Gap C)",
    ),
    (
        # README's own wording. The claim survived four other documents being
        # corrected because it phrases the same assertion differently -- "logs
        # them at INFO", with the card fields named in the preceding clause --
        # so none of the patterns above reached it.
        re.compile(
            r"logs\s+them\s+at\s+INFO|logs\s+(the\s+)?(full\s+)?PAN[^.;|]{0,24}at\s+INFO",
            re.IGNORECASE,
        ),
        "payment-service logs redact_dict output; PaymentIn forbids pan/cvv (ADR 0008)",
    ),
    (
        # The original D5c docstring, verbatim in its load-bearing half. This is
        # the sentence that produced two false findings; it is guarded in the
        # source files as well as the Markdown.
        re.compile(
            r"(logs|writes)\s+the\s+full\s+(charge\s+)?request\s+body", re.IGNORECASE
        ),
        "no service has request-body middleware; logging_config wires handlers only (D5c)",
    ),
    (
        # Servicing's variant of the same docstring, which named the fields
        # instead of saying "PII": "writes the full charge request body (PAN,
        # CVV, SSN) at INFO. No redaction." The clause above catches its opening
        # words; this catches the field list, so a partial restoration of either
        # half fails. `payments.charge()` there logs loan_id/amount/method.
        re.compile(r"\(\s*PAN\s*,\s*CVV\s*,\s*SSN\s*\)\s*at\s+INFO", re.IGNORECASE),
        "servicing's charge logs loan_id/amount/method; it never receives a PAN (ADR 0008)",
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
#
# A blank line ends a clause too: two paragraphs are never one sentence, and
# without that a retraction in one paragraph would reach into the next.
CLAUSE_BOUNDARY = re.compile(
    r"[.;][*_`\"')\]]*\s+"      # sentence end, allowing a closing emphasis mark
    r"|\|\s*"                   # Markdown table cell
    r"|\n[ \t]*\n"              # paragraph break
    # A CONTRASTIVE conjunction ends the clause a cue is allowed to excuse.
    # "payment-service no longer persists PAN, but payment-service logs full
    # PAN at INFO" is one sentence carrying a retraction AND a live claim; the
    # cue belongs to the half before the "but", and splitting only on sentence
    # ends handed the second half an alibi it had not earned. Reviewed on
    # PR #16.
    r"|,?\s*\b(?:but|yet|however|whereas|while|though|although)\b\s*",
    re.IGNORECASE,
)


def _clause_around(text: str, start: int, end: int) -> str:
    """The sentence/cell/paragraph of `text` containing the span [start, end)."""
    cut_before = 0
    cut_after = len(text)
    for boundary in CLAUSE_BOUNDARY.finditer(text):
        if boundary.end() <= start:
            cut_before = boundary.end()
        elif boundary.start() >= end:
            cut_after = boundary.start()
            break
    return text[cut_before:cut_after]


def live_claim_lines(text: str, pattern: re.Pattern) -> list:
    """Line numbers where `pattern` matches with no retraction in its clause.

    Scanned over the WHOLE text, not line by line. Every pattern here joins its
    words with `\\s+`, which matches a newline -- but a per-line scan never gives
    it the chance, so `payment-service logs full\\nPAN at INFO` slipped through
    both halves of a wrapped sentence. These documents wrap prose at roughly 95
    columns as a matter of course, so that was not a corner case: it was most of
    the corpus. Matching across the wrap and reporting the line the match STARTS
    on keeps the failure message pointing at something a reader can open.
    """
    hits = []
    seen_lines = set()
    for match in pattern.finditer(text):
        if RETRACTION_CUES.search(_clause_around(text, match.start(), match.end())):
            continue
        line_no = text.count("\n", 0, match.start()) + 1
        if line_no not in seen_lines:
            seen_lines.add(line_no)
            hits.append(line_no)
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
INFO_LOG_CLAIM = next(p for p, _ in FALSE_CLAIMS if "logs\\s+them" in p.pattern)
REQUEST_BODY_CLAIM = next(p for p, _ in FALSE_CLAIMS if "request\\s+body" in p.pattern)
SERVICING_FIELDS_CLAIM = next(p for p, _ in FALSE_CLAIMS if r"\(\s*PAN" in p.pattern)
FULL_PII_CLAIM = next(p for p, _ in FALSE_CLAIMS if p.pattern == r"logs\s+full\s+PII")

# The exact comment that sat above intake.create_application() before e889255,
# recovered with `git show` rather than paraphrased.
ORIGINATION_COMMENT_BEFORE = (
    "    payload = body.model_dump()\n"
    "    # creates applicant+application rows, logs full PII (D5 — KEEP)\n"
    "    app_id, access_token = intake.create_application(payload)\n"
)


def test_the_origination_call_site_comment_is_caught_if_it_returns():
    """A comment on the line it describes is trusted more than a docstring.

    `logs full PII (D5 — KEEP)` sat directly above the intake call and survived
    every earlier pass, because the guarded corpus was Markdown plus the six
    `logging_config.py` docstrings. The wording also dodges the existing
    origination pattern, which requires the word "origination" -- a comment
    inside origination-service has no reason to say it. Reviewed on PR #16.
    """
    assert live_claim_lines(ORIGINATION_COMMENT_BEFORE, FULL_PII_CLAIM) == [2]


def test_the_origination_router_is_in_the_guarded_corpus():
    """The pattern is worthless if the file it was written for is unscanned."""
    router = REPO_ROOT / "services" / "origination-service" / "app" / "routers" / "applications.py"
    assert router in SOURCES
    assert router.is_file()


def test_the_shipped_router_comment_does_not_carry_that_claim():
    """...and it does not carry it today."""
    router = REPO_ROOT / "services" / "origination-service" / "app" / "routers" / "applications.py"
    assert live_claim_lines(router.read_text(encoding="utf-8"), FULL_PII_CLAIM) == []


def test_a_cue_in_the_other_half_of_a_contrastive_sentence_does_not_excuse_it():
    """One sentence can retract one thing and assert another.

    "payment-service no longer persists PAN, but payment-service logs full PAN
    at INFO" carries a real retraction and a live false claim. The cue belongs
    to the half before the "but"; the clause check used to hand the second half
    that alibi, because both halves were one clause. Reviewed on PR #16.
    """
    mixed = (
        "payment-service no longer persists PAN, but payment-service logs full "
        "PAN at INFO.\n"
    )
    assert live_claim_lines(mixed, PAN_CLAIM) == [1]


def test_a_contrastive_sentence_whose_cue_covers_the_claim_still_passes():
    """The conjunction split must not break an honest retraction either.

    Here the cue sits with the claim it retracts, so nothing is excused that
    should not be.
    """
    honest = (
        "The seed data still writes card values, but this entry previously said "
        "payment-service logs full PAN at INFO, which is false.\n"
    )
    assert live_claim_lines(honest, PAN_CLAIM) == []

# Exactly what `services/servicing-service/app/logging_config.py` said before
# e889255, recovered with `git show e889255~1:` rather than paraphrased. A
# regression test for a specific past wording is worth only as much as its
# fidelity to that wording.
SERVICING_DOCSTRING_BEFORE_D5C = (
    '"""Logging — writes the full charge request body (PAN, CVV, SSN) at INFO. '
    "No redaction.\n\n"
    "Output goes to logs/payment-service.log, the same file handed over in the repo. "
    '(D5, #7)\n"""\n'
)


def test_the_servicing_docstrings_former_wording_is_caught_if_it_returns():
    """The source regression this suite exists for, in the file it came from.

    Servicing's copy of the D5c docstring named the fields rather than saying
    "PII", so neither the "still logs"/"still formats" patterns nor the
    request-body one reached it: restoring it verbatim left the suite green.
    Both of its halves are guarded now, so a partial restoration fails too.
    """
    assert live_claim_lines(SERVICING_DOCSTRING_BEFORE_D5C, REQUEST_BODY_CLAIM) == [1]
    assert live_claim_lines(SERVICING_DOCSTRING_BEFORE_D5C, SERVICING_FIELDS_CLAIM) == [1]


def test_a_claim_wrapped_across_two_lines_is_still_caught():
    """The scan is over the text, not over each line.

    These documents wrap at about 95 columns, so a guarded sentence lands on two
    physical lines as a matter of routine. A per-line scan matched neither half
    and reported nothing -- the quietest possible failure for a guard.
    """
    wrapped = (
        "Storage and logging are both open here: `payment-service` logs full\n"
        "PAN at INFO on every charge, which is a flat PCI violation.\n"
    )
    assert live_claim_lines(wrapped, PAN_CLAIM) == [1]


def test_a_retraction_wrapped_with_its_claim_still_excuses_it():
    """...and the cue may wrap with it, because it is one sentence either way."""
    wrapped = (
        "This entry previously said that `payment-service` logs full\n"
        "PAN at INFO; it is false against the current code.\n"
    )
    assert live_claim_lines(wrapped, PAN_CLAIM) == []


def test_a_retraction_in_the_previous_paragraph_does_not_reach_forward():
    """A blank line ends the clause -- otherwise wrap-tolerance reopens the hatch."""
    two_paragraphs = (
        "This entry previously said several things that were false.\n"
        "\n"
        "`payment-service` logs full PAN at INFO.\n"
    )
    assert live_claim_lines(two_paragraphs, PAN_CLAIM) == [3]


def test_the_readme_logging_claim_is_caught_if_it_returns():
    """README outlived four corrected documents by wording it differently.

    Its Compliance section said `payment-service` "persists the full PAN and
    CVV ... and logs them at INFO". The storage half is true and stays; the
    logging half was false, and no pattern written for the other documents
    came close to matching this phrasing -- which is how the most-read file in
    the repository ended up as the last one still asserting it.
    """
    regressed = (
        "**Not PCI-DSS compliant** -- `payment-service` persists the full PAN "
        "and CVV unencrypted and logs them at INFO."
    )
    assert live_claim_lines(regressed, INFO_LOG_CLAIM) == [1]


def test_the_readme_is_in_the_guarded_corpus():
    """The pattern above is worthless if the file it was written for is unscanned."""
    assert (REPO_ROOT / "README.md") in DOCS

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


def test_the_request_schema_rejects_the_card_fields_by_name():
    """The code half of the same claim -- stated at its real strength.

    This proves exactly one thing: no FIELD CALLED pan/cvv/ssn is accepted, and
    a client that sends one gets a 422 rather than a silent drop. It does NOT
    prove that no card number can enter the process, and an earlier version of
    this docstring said it did. `extra="forbid"` matches on field names, not on
    content, so `processor_token="4111111111111111"` is a perfectly valid
    payload -- which is why the content guarantee is a separate test below and
    a separate sentence in the documents.
    """
    fields = set(schemas.PaymentIn.model_fields)
    for forbidden in ("pan", "cvv", "ssn"):
        assert forbidden not in fields, (
            f"PaymentIn accepts {forbidden!r} again -- the documents this test "
            f"guards would now be telling the truth"
        )
    assert schemas.PaymentIn.model_config.get("extra") == "forbid"


def test_card_data_smuggled_through_an_allowed_field_is_redacted_before_logging():
    """The guarantee that actually covers the gap the schema check leaves.

    A caller can put a PAN in `processor_token`, or an SSN in `brand`, and the
    schema will accept it: those are allowed fields and pydantic validates
    shape, not sensitivity. What stops it reaching the log is `charge()`
    building its line through `redact_dict`, which masks sensitive KEYS
    outright and runs the PAN/SSN/CVV patterns over every other string VALUE.
    Exercised on the same dict shape `charge()` logs.
    """
    safe = redact_dict({
        "processor_token": "4111111111111111",   # a real-format PAN, Luhn-valid
        "last4": "1111",
        "brand": "cardholder ssn 123-45-6789",
        "amount": 10.0,
        "loan_id": 1,
        "idempotency_key": "k",
    })
    rendered = str(safe)
    # By key: a vaulted token is sensitive whatever it contains, so it is
    # replaced wholesale rather than pattern-matched.
    assert safe["processor_token"] == "[REDACTED]"
    # By content: an allowed free-text field carrying a PAN or an SSN is masked
    # on the way through, which is the half `extra="forbid"` cannot do.
    assert "4111111111111111" not in rendered
    assert "123-45-6789" not in rendered
    assert "[SSN-REDACTED]" in safe["brand"]
    # Non-sensitive context survives -- a redactor that ate the correlation
    # fields would be traded for an unusable log.
    assert safe["last4"] == "1111" and safe["loan_id"] == 1


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


# --- what the seeds actually WRITE -------------------------------------------
#
# Reviewed on PR #16. The first version of this guard read the `payments` INSERT
# column list and checked that `pan` and `cvv` were absent. That is not the claim
# README makes. A 16-digit literal in `last4` or `brand`, or a PAN inside an
# `audit_logs` message -- which is the exact historical exposure this branch
# documents, `charge req pan=4111111111111111` -- passes a column-name check and
# still puts card data in every fresh database. So the values are scanned, in
# every INSERT, into every table.

_SEED_FILES = ("002_seed.sql", "003_seed_bulk.sql")

# Any 13-19 digit run, Luhn-valid, however it is punctuated. The redactor's own
# definition, reused deliberately: a second private notion of "looks like a PAN"
# in a test would drift from the one the service enforces.
_INSERT_RE = re.compile(
    r"INSERT\s+INTO\s+(\w+)\s*\(([^)]*)\)\s*(?:VALUES|SELECT)(.*?)(?=;\s*(?:--|$|\n))",
    re.IGNORECASE | re.DOTALL,
)
_LITERAL_RE = re.compile(r"'((?:[^']|'')*)'")


def _seed_statements():
    """Every INSERT in the seed files, as (file, table, columns, literal values)."""
    found = []
    for name in _SEED_FILES:
        sql = (REPO_ROOT / "db" / "init" / name).read_text(encoding="utf-8")
        # Strip line comments first: they discuss PAN legitimately, and the
        # question is what the statement WRITES.
        stripped = re.sub(r"--[^\n]*", "", sql)
        for match in _INSERT_RE.finditer(stripped):
            table = match.group(1).lower()
            columns = [c.strip().lower() for c in match.group(2).split(",")]
            literals = _LITERAL_RE.findall(match.group(3))
            found.append((name, table, columns, literals))
    return found


def test_no_seed_statement_writes_a_card_number_into_any_table():
    """README's claim, checked against the values rather than the column names.

    Covers `last4`/`brand` and every other seed target including `audit_logs`,
    because a PAN in a log message is card data in a fresh database no matter
    which column it sits in.
    """
    from app import redactor

    offenders = []
    for name, table, columns, literals in _seed_statements():
        for value in literals:
            if redactor.looks_like_pan(value):
                offenders.append(f"{name} -> {table}: {value!r}")
    assert not offenders, (
        "seed statements write card or personal data:\n  "
        + "\n  ".join(offenders)
        + "\n\nREADME and docs/DEBT.md D5b state that a freshly initialised "
          "database contains no card data. Either the seed is wrong or those "
          "documents are; do not silence this test."
    )


def test_no_seed_statement_names_a_card_column():
    """The column-name check, kept as well -- the two catch different mistakes.

    A value scan misses `pan = <a parameter>` or a non-Luhn placeholder; a column
    check misses a real PAN in `brand`. Neither subsumes the other.
    """
    offenders = [
        f"{name} -> {table}({', '.join(columns)})"
        for name, table, columns, _ in _seed_statements()
        if {"pan", "cvv"} & set(columns)
    ]
    assert not offenders, "seed statements write to a card column:\n  " + "\n  ".join(offenders)


def test_the_seed_scan_sees_every_table_the_seeds_write():
    """Guard against the scan reading nothing and reporting clean.

    Names the tables that must appear, so a broken regex or a renamed file cannot
    make the two tests above vacuous. `audit_logs` is listed explicitly because it
    is the table the original exposure was in and the one a column-name check
    could never have covered.
    """
    tables = {table for _, table, _, _ in _seed_statements()}
    for required in ("payments", "audit_logs", "offers", "loans", "applications"):
        assert required in tables, (
            f"the seed scan never saw an INSERT into {required!r}; it found "
            f"{sorted(tables)} -- the parser is broken and the guards above prove nothing"
        )


def test_the_only_seeded_personal_data_is_the_ssn_debt_this_register_already_names():
    """The scan above asks about CARD data. This states what else it finds, so the
    scope is visible rather than implied.

    `applicants.ssn` holds fictional SSNs on purpose -- plaintext SSN storage is
    its own entry in docs/DEBT.md and is not what README's card-data claim is
    about. Pinning it here means an SSN appearing in some OTHER column, where
    nobody has accounted for it, fails a test instead of passing quietly.
    """
    from app import redactor

    unexpected = []
    for name, table, columns, literals in _seed_statements():
        for value in literals:
            if not redactor.looks_sensitive(value) or redactor.looks_like_pan(value):
                continue
            if table == "applicants" and "ssn" in columns:
                continue          # the documented, separately tracked debt
            unexpected.append(f"{name} -> {table}({', '.join(columns)}): {value!r}")
    assert not unexpected, (
        "seed statements write personal data outside the one place this "
        "repository accounts for it:\n  " + "\n  ".join(unexpected)
    )


@pytest.mark.parametrize("value,expected", [
    ("4111111111111111", True),                 # Luhn-valid PAN, bare
    ("charge req pan=4111111111111111 amount=250.00", True),   # the historical audit row
    ("4111-1111-1111-1111", True),              # punctuated
    ("412-55-9981", False),                     # an SSN is not a card number
    ("1111", False),                            # last4, which is permitted
    ("visa", False),                            # brand
    ("charge req last4=1111 amount=250.00", False),            # the current audit row
    ("1234567890123456", False),                # 16 digits, Luhn-INVALID -- not a card
])
def test_the_seed_scanner_recognises_card_data_and_leaves_the_rest_alone(value, expected):
    """Positive and negative fixtures for the detector itself.

    Without these, "no offenders" is indistinguishable from "detects nothing".
    The last case is the interesting negative: length alone does not make a card
    number, and a scanner that flagged it would fail on ordinary seed integers.
    """
    from app import redactor

    assert redactor.looks_like_pan(value) is expected, value


def test_a_real_initialised_database_holds_no_card_data():
    """The claim checked against a database, not against the SQL that builds one.

    Review of PR #16 asked for this if practical, and it is: `db/init` is applied
    into a throwaway schema and every text-ish column of every table it created is
    scanned for a Luhn-valid card number. Parsing SQL can be fooled -- by a
    generated value, an `INSERT ... SELECT`, or a statement shape the regex does
    not match -- and this cannot, because it reads what actually landed.

    Skipped when DATABASE_URL is unset, so the SQL-level guards above remain the
    always-on floor.
    """
    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        pytest.skip("DATABASE_URL not set -- no Postgres to initialise")

    import psycopg2

    from app import redactor

    schema = "seed_content_scan"
    init_dir = REPO_ROOT / "db" / "init"
    files = sorted(p for p in init_dir.glob("*.sql"))
    assert files, "no db/init SQL found -- this test would prove nothing"

    conn = psycopg2.connect(database_url)
    conn.autocommit = True
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            cur.execute(f"CREATE SCHEMA {schema}")
            cur.execute(f"SET search_path TO {schema}")
            for path in files:
                cur.execute(path.read_text(encoding="utf-8"))

            cur.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND data_type IN "
                "('text','character varying','character','json','jsonb')",
                (schema,),
            )
            columns = cur.fetchall()
            assert columns, "the initialised schema has no text columns -- scan is vacuous"

            offenders = []
            scanned_rows = 0
            for table, column in columns:
                cur.execute(
                    f'SELECT "{column}"::text FROM "{table}" '
                    f'WHERE "{column}" IS NOT NULL'
                )
                for (value,) in cur.fetchall():
                    scanned_rows += 1
                    if redactor.looks_like_pan(value):
                        offenders.append(f"{table}.{column}: {value!r}")

        assert scanned_rows > 0, (
            "no seeded text values were scanned; db/init produced an empty "
            "database and this test proves nothing"
        )
        assert not offenders, (
            "a freshly initialised database contains card data:\n  "
            + "\n  ".join(offenders)
            + "\n\nREADME and docs/DEBT.md D5b both state it does not."
        )
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
        conn.close()
