"""Both readers of the note rate must apply the same rule, and nothing may read
the column that was dropped.

Two services answer "what rate is this loan billed at": servicing, for staff and
for the loan detail page, and the gateway, for a borrower's own list. They read
the same rows and must not disagree -- a borrower seeing 7.99% on one screen and
"not recorded" on another is the D19 confusion wearing a new costume.

**What this file asserted during the expand phase, and why it changed.** The rule
then had three branches, because `loans.apr` held either the contractual note
rate or the disclosed APR depending on the boarding path:

  1. `note_rate_pct` present              -> that value, proven
  2. NULL but `schedule_version` present  -> `apr`, proven
  3. neither                              -> unknown, "not recorded"

Branch 2 was the rolling-deploy fallback and this file required it, on the
grounds that deleting it early would silently report "not recorded" for loans an
older image boarded. `db/migrations/0039` dropped `apr` and made `note_rate_pct`
NOT NULL, so branches 2 and 3 now describe a column and a state that do not
exist -- and the tests requiring them are inverted below rather than deleted,
because the reason each existed is what makes the inversion checkable.

The gate in 0039 that asks an operator to confirm no deployed image still reads
`loans.apr` rests on `test_no_service_source_reads_the_retired_column`. Read
statically and derived from source: importing two services into one process is
what broke a fixture in this suite before, and a hand-maintained list of readers
has gone stale in this repository twice.
"""
import ast
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SERVICES = REPO / "services"
SERVICING = SERVICES / "servicing-service" / "app" / "routers" / "loans.py"
GATEWAY = SERVICES / "gateway" / "app" / "main.py"


def _service_sources():
    """Every non-test service source file. Derived, never listed."""
    for path in sorted(SERVICES.rglob("*.py")):
        if "__pycache__" in str(path) or "/tests/" in path.as_posix():
            continue
        yield path


def _servicing_rule() -> str:
    """The rule as CODE. The docstring is stripped deliberately: this file's
    whole subject is prose that outlived the code it described, and a docstring
    explaining why `schedule_version` used to matter would otherwise fail the
    assertions below for containing the word."""
    tree = ast.parse(SERVICING.read_text(encoding="utf-8"))
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_proven_note_rate")
    body = fn.body
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    return "\n".join(ast.unparse(stmt) for stmt in body)


def _gateway_rule() -> str:
    """Same, for the gateway's borrower row.

    Bounded by the two note-rate keys rather than by a character count. A fixed
    window ran past them into the next field, and `opened_at`'s own `else None`
    then read as an unknown-RATE branch -- a test failing on a neighbouring
    line's code is the vacuity problem inverted.
    """
    lines = GATEWAY.read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(lines) if '"note_rate_pct":' in line)
    end = next(i for i, line in enumerate(lines[start:], start)
               if '"note_rate_proven"' in line)
    return "\n".join(line for line in lines[start:end + 1]
                     if not line.strip().startswith("#"))


# --- nothing reads the dropped column ----------------------------------------
#
# `offers.apr` still exists and is correctly named, so a blanket search for the
# word would be useless. These look for `apr` used against LOANS specifically:
# a SQL string that names both, and attribute access on a loan-shaped object.

_QUALIFIED_LOAN_APR = re.compile(r"\b(l|loans)\.apr\b", re.IGNORECASE)
_BARE_APR = re.compile(r"\bapr\b", re.IGNORECASE)
_LOAN_ATTR = re.compile(r"^(loan|l|row|r)$|loan$", re.IGNORECASE)
# A statement whose WRITE TARGET is `offers`. Its `SET`/column-list names cannot
# be table-qualified -- `SET offers.apr = ...` is a syntax error in Postgres --
# so "qualify it" is not advice that can be followed there, and a bare `apr` in
# such a statement unambiguously belongs to `offers`.
_WRITES_OFFERS = re.compile(r"\b(?:UPDATE|INSERT\s+INTO)\s+offers\b", re.IGNORECASE)


def _reads_retired_loan_apr(sql: str) -> bool:
    """Does this statement read `apr` off `loans`?

    The rule, in order:

      1. Not about loans at all               -> no.
      2. `l.apr` / `loans.apr`                -> yes, unambiguous.
      3. Writes to `offers`                   -> no; a bare `apr` there is the
         offer's and cannot be qualified.
      4. Any remaining UNQUALIFIED `apr`      -> yes.

    Rule 4 is deliberately strict. It used to skip any statement naming both
    tables, which left `SELECT apr FROM loans JOIN offers ...` unflagged -- the
    exact query that breaks once `apr` is gone from `loans`, and the one most
    likely to be written by someone reaching for the offer's APR. A qualified
    read passes by SAYING which table it means; a bare one is reported even if
    it turns out to be the offer's, because a reader cannot tell either and the
    fix is one word. (Review of PR #37.)
    """
    if not re.search(r"\bloans\b", sql, re.IGNORECASE):
        return False
    if _QUALIFIED_LOAN_APR.search(sql):
        return True
    if _WRITES_OFFERS.search(sql):
        return False
    return bool(_BARE_APR.search(re.sub(r"\b\w+\.apr\b", "", sql,
                                        flags=re.IGNORECASE)))


def _docstring_nodes(tree):
    """Every string that is a docstring, so prose is never mistaken for SQL.

    This file exists because a claim outlived its code. The history of `apr` is
    kept in docstrings deliberately, and a scanner that read them would report
    the documentation of a removed defect as the defect itself.
    """
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                             ast.ClassDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                out.add(id(body[0].value))
    return out


def _sql_strings_touching_loans(tree):
    docstrings = _docstring_nodes(tree)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            value = node.value
            if re.search(r"\bloans\b", value, re.IGNORECASE):
                yield value


def _loan_apr_attribute_reads(tree):
    for node in ast.walk(tree):
        if (isinstance(node, ast.Attribute) and node.attr == "apr"
                and isinstance(node.value, ast.Name)
                and _LOAN_ATTR.search(node.value.id)):
            yield f"{node.value.id}.apr (line {node.lineno})"


def test_no_service_source_reads_the_retired_column():
    """What 0039's operator acknowledgement is checked against.

    A file may still MENTION `apr` -- the history is deliberately kept in
    comments and docstrings, and `offers.apr` is a real disclosed APR that
    stays. What must not survive is executable code reading it off a loan.
    """
    offenders = []
    scanned = 0
    for path in _service_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        scanned += 1
        rel = path.relative_to(REPO).as_posix()

        for sql in _sql_strings_touching_loans(tree):
            if _reads_retired_loan_apr(sql):
                offenders.append(f"{rel}: SQL reads apr off loans -- {sql[:90]!r}")

        for read in _loan_apr_attribute_reads(tree):
            offenders.append(f"{rel}: {read}")

    assert scanned > 20, (
        f"only {scanned} service files scanned -- the sweep found almost nothing "
        f"to check, so a pass here proves nothing"
    )
    assert not offenders, (
        "service code still reads the dropped column `loans.apr`; running "
        "db/migrations/0039 would break it:\n  " + "\n  ".join(offenders)
    )


def test_no_browser_test_queries_the_retired_column():
    """The same check, over `frontend/e2e`, because the Python sweep could not
    see it and CI found what it missed.

    `test_no_service_source_reads_the_retired_column` scans service source only.
    The e2e specs query the real database directly to verify what was boarded,
    so they read the schema exactly as a deployed service does -- and one of
    them still selected `apr` from `loans` after every service had been
    converted. It failed in CI, on this PR, which is the honest reason this
    test exists rather than a class of defect anticipated in advance.

    `offers.apr` is a real disclosed APR and those queries stay.
    """
    e2e = REPO / "frontend" / "e2e"
    offenders = []
    scanned = 0
    for path in sorted(e2e.rglob("*.ts")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        scanned += 1
        # Join adjacent string literals first. The query that broke CI is split
        # across two of them -- `"SELECT apr, ... "` + `"... FROM loans ..."` --
        # so a per-literal scan sees `apr` and `loans` in different strings and
        # reports nothing. Mutation-testing this check is what exposed that: the
        # first version passed with the bad query put back.
        text = re.sub(r'"\s*\+\s*"', "", text)
        for statement in re.findall(r'"[^"]*\bloans\b[^"]*"', text, re.IGNORECASE):
            if re.search(r"\boffers\b", statement, re.IGNORECASE):
                continue
            if _QUALIFIED_LOAN_APR.search(statement) or _BARE_APR.search(statement):
                offenders.append(
                    f"{path.relative_to(REPO).as_posix()}: {statement[:90]}")

    assert scanned, "no e2e specs found -- the sweep is looking in the wrong place"
    assert not offenders, (
        "an e2e spec still queries `loans.apr`, which no longer exists:\n  "
        + "\n  ".join(offenders)
    )


def test_the_scanner_catches_the_shapes_it_is_meant_to():
    """The scanner is checked against known-bad and known-good SQL directly.

    Every other test here passes when the codebase is clean, which is also what
    a broken scanner does. These cases pin the rule itself, and the third is the
    hole review of PR #37 found: a statement naming BOTH tables used to be
    skipped entirely, so `SELECT apr FROM loans JOIN offers ...` -- the one
    query most likely to be written by someone reaching for the offer's APR and
    getting the loan's -- sailed through.
    """
    flagged = _reads_retired_loan_apr

    must_flag = [
        "SELECT l.apr FROM loans l",
        "SELECT apr FROM loans WHERE id = 1",
        # The hole the review found: names both tables, so it used to be skipped.
        "SELECT apr, note_rate_pct FROM loans JOIN offers ON offers.app_id = loans.app_id",
        "SELECT loans.apr FROM loans JOIN offers ON offers.app_id = loans.app_id",
    ]
    must_pass = [
        "SELECT note_rate_pct FROM loans",
        "SELECT o.apr FROM loans l JOIN offers o ON o.app_id = l.app_id",
        "SELECT offers.apr FROM offers JOIN loans ON loans.app_id = offers.app_id",
        "SELECT apr FROM offers WHERE app_id = 1",
        # A write to `offers` whose SET column CANNOT be qualified -- Postgres
        # rejects `SET offers.apr = ...` outright, so 'qualify it' is not advice
        # that can be followed. This is the real statement in
        # `disclosure-service/app/routers/offers.py`, which the first version of
        # the tightened rule reported as a retired-column read.
        "WITH repaired AS (UPDATE offers o SET note_rate_pct = %s, apr = %s "
        "WHERE app_id = %s RETURNING o.id) SELECT id FROM repaired "
        "JOIN loans ON loans.app_id = %s",
        "INSERT INTO offers (app_id, apr, note_rate_pct) "
        "SELECT app_id, %s, %s FROM loans WHERE id = %s",
    ]
    for sql in must_flag:
        assert flagged(sql), f"the scanner missed a retired-column read: {sql!r}"
    for sql in must_pass:
        assert not flagged(sql), f"the scanner reported a legitimate query: {sql!r}"


def test_the_scan_can_still_see_the_offer_column():
    """Guard the guard. The check above passes trivially if the scanner stopped
    finding anything at all -- a changed AST shape, a moved directory. `offers.apr`
    is real, present and must NOT be reported, so finding it proves the scanner
    is reading code rather than returning an empty set."""
    found = []
    for path in _service_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if re.search(r"\bapr\b", node.value, re.IGNORECASE):
                    found.append(path.relative_to(REPO).as_posix())
                    break
    assert found, (
        "the scanner found no `apr` anywhere in service SQL, including the "
        "offers column that legitimately exists -- it is not reading code"
    )


# --- the two readers still agree ---------------------------------------------

def test_both_readers_use_the_column_that_says_what_it_holds():
    for name, rule in (("servicing", _servicing_rule()), ("gateway", _gateway_rule())):
        assert "note_rate_pct" in rule, (
            f"{name} does not consult loans.note_rate_pct, so it is reporting a "
            f"regulated figure from somewhere other than the column that holds it"
        )


def test_neither_reader_still_infers_the_rate_from_schedule_version():
    """The inversion of this file's old `..._keep_the_rolling_deploy_fallback`.

    `schedule_version` was evidence about WHICH figure `loans.apr` held. With
    that column gone it says nothing about the rate, so branching on it would
    withhold a number every loan now has -- and would do it silently, which is
    how the original defect survived as long as it did.
    """
    for name, rule in (("servicing", _servicing_rule()), ("gateway", _gateway_rule())):
        assert "schedule_version" not in rule, (
            f"{name} still branches on schedule_version. Since 0039 that proves "
            f"nothing about the rate, and the loans it withholds are ones whose "
            f"note_rate_pct is NOT NULL and known"
        )
        assert "apr" not in rule, (
            f"{name} still references the dropped `apr` column in the rule"
        )


def test_neither_reader_can_report_an_unknown_rate():
    """Also an inversion. Servicing's `_proven_note_rate` used to have a
    `(None, False)` branch and the gateway an `else None`; both existed because a
    legacy row's figure could not be proven. 0039's gate 1 refused to run while
    any such row existed, so the branch is unreachable by construction -- and an
    unreachable "not recorded" branch is a claim no reader can verify."""
    servicing = _servicing_rule()
    assert "None, False" not in servicing and "(None, False)" not in servicing, (
        "servicing still has an unknown-rate branch, but loans.note_rate_pct is "
        "NOT NULL -- the branch cannot be reached and cannot be tested"
    )
    assert "else None" not in _gateway_rule(), (
        "the gateway still has an unknown-rate branch"
    )


def test_a_new_reader_cannot_appear_unnoticed():
    """A map held to the code so it cannot go stale.

    The last time a hand-written list of readers was trusted in this repository
    it was missing one, twice. This derives the list and fails on anything not
    accounted for -- so a third reader of the note rate has to be looked at
    rather than assumed to agree with the other two.
    """
    readers = []
    for path in _service_sources():
        text = path.read_text(encoding="utf-8", errors="ignore")
        if "note_rate_pct" in text:
            readers.append(path.relative_to(REPO).as_posix())

    accounted_for = {
        # The two that apply the rule.
        "services/gateway/app/main.py",
        "services/servicing-service/app/routers/loans.py",
        # Schema/model/boarding files that name the column without deciding
        # anything about it.
        "services/servicing-service/app/models.py",
        "services/servicing-service/app/schemas.py",
        "services/origination-service/app/intake.py",
        "services/origination-service/app/routers/applications.py",
        "services/disclosure-service/app/routers/offers.py",
        "services/disclosure-service/app/models.py",
        # These name `offers.note_rate_pct`, which has existed since 0030 and is
        # a different column from the one on loans. Listed rather than filtered
        # out by a path rule, because "it is probably the offer one" is the kind
        # of assumption this file exists to stop.
        "services/origination-service/app/models.py",
        "services/origination-service/app/routers/offers.py",
        "services/origination-service/app/schemas.py",
        "services/disclosure-service/app/schemas.py",
    }
    assert readers, "no file mentions note_rate_pct -- the sweep is broken"
    unexpected = set(readers) - accounted_for
    assert not unexpected, (
        f"a new note-rate reader appeared in {sorted(unexpected)}. Check it "
        f"agrees with the other two before adding it here: a reader that "
        f"withholds a rate the others report is the D19 confusion returning."
    )
