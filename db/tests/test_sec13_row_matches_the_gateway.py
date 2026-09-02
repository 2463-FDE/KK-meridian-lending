"""SEC-13's row has to keep describing the gateway that exists.

WHY THIS FILE EXISTS. PR #158 removed the raw-upstream-body passthrough from
`gateway/app/main.py` -- 112 lines changed, 308 lines of tests added -- and
changed no documentation. For a day `docs/DEBT.md` SEC-13 still read
`OPEN ENGINEERING GAP` and still described the passthrough in the present tense,
so the register reported a live gateway information-disclosure gap that had been
closed. Nothing failed, because no test tied a SEC row to the code it describes.

`test_historical_findings_do_not_look_open.py` catches a row that presents a
dated finding as current status, and `test_docs_do_not_describe_deleted_code.py`
catches a document naming code that no longer exists. Neither catches this
shape: a row whose STATUS is stale while its prose still parses as a description
of something real. This does, for the one row where the mismatch was actually
made.

Deliberately about SEC-13 alone rather than a scheme for all seventeen rows. A
generic "every row's status matches its code" check would need a machine-readable
link from each row to its control, which does not exist and would be invented
here rather than derived -- and an invented mapping is the kind of guard that
passes while meaning nothing (SEC-11's Next.js acceptance was exactly that
mistake). One row, one real relationship, checkable.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
DEBT = REPO / "docs" / "DEBT.md"
GATEWAY_MAIN = REPO / "services" / "gateway" / "app" / "main.py"


def _sec13_row() -> str:
    for line in DEBT.read_text(encoding="utf-8").splitlines():
        if line.startswith("| **SEC-13**"):
            return " ".join(line.split())
    raise AssertionError("no SEC-13 row in docs/DEBT.md")


def _current_status() -> str:
    """The row's STATUS column with its quoted history removed.

    This register keeps what a row USED to say, in italics, so a correction is
    visible rather than silent -- SEC-13's status opens by quoting its own former
    `OPEN ENGINEERING GAP, narrow / NEEDS RUNTIME VERIFICATION` wording. A check
    that grepped the whole column would fail on exactly the sentence that proves
    the row was corrected, which is the guard being wrong rather than the row.

    Single asterisks delimit that history; `**` is emphasis and stays. What is
    left is what the row asserts NOW, which is what these cases are about.
    """
    cols = [c.strip() for c in _sec13_row().strip("|").split("|")]
    assert len(cols) >= 3, f"SEC-13 row has too few columns: {len(cols)}"
    return re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", "", cols[2], flags=re.DOTALL)


def _gateway_code() -> str:
    """The gateway's source with comments and docstrings stripped.

    The fix's own comment quotes the old `{"raw": ...}` return in order to record
    what it replaced, which is exactly the history this register keeps. A check
    that grepped the raw file would therefore fail on the comment that documents
    the fix -- so what is searched is code.
    """
    text = GATEWAY_MAIN.read_text(encoding="utf-8")
    # Triple-quoted docstrings first, then line comments.
    text = re.sub(r'"""(?:.|\n)*?"""', "", text)
    text = re.sub(r"^\s*#.*$", "", text, flags=re.MULTILINE)
    return text


def test_the_gateway_does_not_return_an_upstream_body_to_the_caller():
    """The control itself. Everything below is about describing it correctly."""
    code = _gateway_code()
    assert '"raw"' not in code, (
        'the gateway returns a `{"raw": ...}` body again, which is the SEC-13 '
        "passthrough #158 removed -- an unbounded reflection of an upstream body "
        "to an external caller")
    assert "_json_or_refuse" in code, (
        "`_json_or_refuse` is gone, so whatever replaced it needs re-triaging "
        "before SEC-13 can keep saying the passthrough is closed")


def test_the_row_quotes_the_refusal_callers_actually_get():
    """The row must name the reply, not paraphrase it.

    `_UNREADABLE_DETAIL` is what an external caller receives. If the constant is
    reworded and the row is not, SEC-13 starts quoting a string that no longer
    exists -- the same failure as citing a file that has been renamed.
    """
    code = GATEWAY_MAIN.read_text(encoding="utf-8")
    m = re.search(r'_UNREADABLE_DETAIL\s*=\s*"([^"]+)"', code)
    assert m, "gateway no longer defines _UNREADABLE_DETAIL"
    detail = m.group(1)
    assert detail in _sec13_row(), (
        f"SEC-13 does not quote the refusal the gateway actually sends ({detail!r}), "
        "so a reader cannot check the row against the code")


def test_the_row_does_not_call_a_closed_gap_open():
    """The specific staleness that happened.

    Scoped to the STATUS column: the problem column keeps its original finding
    as written, which is this register's convention and is why the word "OPEN"
    cannot simply be banned from the whole row.
    """
    status = _current_status().upper()

    code = _gateway_code()
    passthrough_present = '"raw"' in code

    if not passthrough_present:
        assert "OPEN ENGINEERING GAP" not in status, (
            "the gateway no longer reflects an upstream body, but SEC-13 still "
            "calls itself an OPEN ENGINEERING GAP")
        assert "FIXED" in status, (
            "SEC-13's status should record the fix once the passthrough is gone")
    else:
        assert "FIXED" not in status, (
            "SEC-13 claims to be fixed while the gateway still returns a raw "
            "upstream body")


def test_the_row_states_what_the_fix_does_not_cover():
    """A closed row still has to bound its claim.

    "The gateway does not reflect an unparseable upstream body" is narrower than
    "no service leaks anything in an error body", and a reader skimming a FIXED
    row will take the wider reading unless the narrower one is written down.
    """
    row = _sec13_row()
    assert "LIMITATION" in row.upper(), (
        "SEC-13 records no limitation, so a reader could take it as a general "
        "output-encoding guarantee rather than a bound on what the gateway "
        "reflects")


def test_the_status_marker_and_the_footer_agree_about_this_row():
    """SEC-13 was verified by running the stack, so the marker must be gone.

    `test_security_register_marks_what_it_cannot_prove.py` holds the footer and
    the table to each other in general; this asserts the direction that matters
    here -- a row whose runtime question has been answered must not still promise
    the answer.
    """
    status = _current_status().upper()
    assert "NEEDS RUNTIME VERIFICATION" not in status, (
        "SEC-13 still promises runtime verification, which was supplied when the "
        "stack was brought up and an upstream 307 returned the refusal rather "
        "than the body")


@pytest.mark.parametrize("needle", ["204", "304", "502"])
def test_the_row_records_the_status_semantics_it_chose(needle):
    """The 502 in particular is a behaviour change a reader has to be able to find.

    `_json_or_refuse` preserves body-less and error statuses and turns an
    unreadable SUCCESS into 502. That last one also makes an upstream 3xx a 502,
    which is why the row has to say so rather than leave it to be discovered by
    whoever proxies a redirect next.
    """
    assert needle in _sec13_row(), (
        f"SEC-13 does not mention {needle}, so its status semantics cannot be "
        "checked against `_json_or_refuse`")
