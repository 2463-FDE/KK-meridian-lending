"""`frontend/e2e/README.md`'s write inventory must match the tree.

**THIS TABLE HAS BEEN WRONG TWICE, THE SAME WAY.** It is the inventory a reader
consults to answer "what does the browser suite mutate", and the section it sits
in exists specifically to correct an earlier false claim that the suite was
read-only.

*First* it said "seven" and omitted `servicing-raises-a-proposal` and
`regeneration-reprices-the-offer`, because the command it was built from had been
truncated with `head`.

*Then* it omitted `approvals-resolved-history` -- which the paragraphs below the
table already discussed at length as an append-only writer -- and the
`fixtures.ts` writes the fixture helper had just gained. Found by review, not by
anything mechanical.

Giving the re-derive command in the README was the fix chosen the first time, and
it was not enough: a command a reader may run is not a check that runs. So this
re-derives the set and compares.

**A NOTE ON THE COMMAND, because getting it wrong is how the over-count
happened.** The obvious `grep -o` prints only the matched SQL and therefore
discards the `//` or `*` that marks a comment. Two matches in this suite are
prose -- `UPDATE balances` in `fee-waiver-clarity`, describing what the fixture
did before PR #113, and `UPDATE decisions` in `decision-evidence`, in a comment
explaining what a staff override does. Neither spec performs that write. A
`-o`-based command cannot tell them from code, so this reads whole lines and
drops comment lines, exactly as the README now documents.

**Scope, stated so the assertion is not read as wider than it is.** This checks
that the documented inventory names the same FILES the tree writes from. It does
not verify the "Writes" column's SQL verb-and-table detail against the source:
that column carries editorial grouping a table wants and a grep does not
produce -- `INSERT` / `DELETE payments` on one line, the `fee_assessed`
parenthetical. Holding a human-readable column to a machine-generated string
would force the table to be written the way grep prints, which makes it worse to
read. The file set is what went wrong twice, and the file set is what is pinned.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
E2E = REPO / "frontend" / "e2e"
README = E2E / "README.md"

_WRITE = re.compile(r"(INSERT INTO|UPDATE|DELETE FROM)\s+[a-z_]+")

#: A line whose first non-space character starts a comment. `*` catches the
#: continuation lines of a block comment, which is where both prose matches live.
_COMMENT = re.compile(r"^\s*(//|\*|/\*)")


def _files_that_write():
    """Every `frontend/e2e/*.ts` file containing a real SQL write."""
    found = set()
    for path in sorted(E2E.glob("*.ts")):
        for line in path.read_text(encoding="utf-8").splitlines():
            if _COMMENT.match(line):
                continue
            if _WRITE.search(line):
                found.add(path.name)
                break
    return found


def _files_named_in_the_readme():
    """The file names the README's two inventory tables name.

    Both tables are read -- the spec table and the helper table below it -- so
    the helper being listed separately is honoured rather than treated as an
    omission. That split is deliberate: `fixtures.ts` is not a spec, and its
    writes belong to whichever test calls it.
    """
    text = README.read_text(encoding="utf-8")
    named = set()
    for row in re.finditer(r"^\s*\|\s*`([^`]+)`", text, re.M):
        entry = row.group(1)
        # Table rows carry a bare spec name (`fee-waiver-clarity`) or a real
        # filename (`fixtures.ts`). Anything else in a first cell is not an
        # inventory row.
        if entry.endswith(".ts"):
            named.add(entry)
        elif re.fullmatch(r"[a-z0-9-]+", entry):
            named.add(entry + ".spec.ts")
    return named


def test_the_readme_still_has_an_inventory_to_check():
    """Guard the guard.

    If the tables were removed or reshaped, the comparison below would run over
    an empty set and pass for having checked nothing -- which is the same class
    of failure as the omissions it exists to catch.
    """
    assert README.is_file(), "frontend/e2e/README.md is missing"
    named = _files_named_in_the_readme()
    assert len(named) >= 8, (
        "only %d inventory rows parsed out of frontend/e2e/README.md. If the "
        "tables moved or changed shape, point this test at them -- do not let "
        "it pass by finding nothing. Parsed: %s"
        % (len(named), sorted(named)))


def test_the_sweep_finds_writers():
    """Guard the guard, other direction.

    A broken pattern would report that nothing writes, which would make the
    inventory trivially complete and reassert the false "read-only" claim this
    README section was written to correct.
    """
    writers = _files_that_write()
    assert len(writers) >= 8, (
        "only %d e2e files appear to write SQL. The pattern has stopped "
        "matching, and an empty result would make any inventory look complete: "
        "%s" % (len(writers), sorted(writers)))


def test_every_writer_is_named_in_the_inventory():
    """The omission that happened, twice."""
    missing = sorted(_files_that_write() - _files_named_in_the_readme())
    assert not missing, (
        "frontend/e2e/README.md's write inventory does not name %s, which do "
        "write to the database. A reader consulting that table to answer 'what "
        "does this suite mutate' gets an answer that is missing entries -- the "
        "same defect the table has already had twice. Re-derive with the "
        "command the README documents and add the rows." % missing)


def test_the_inventory_names_nothing_that_does_not_write():
    """The other direction, which matters for a different reason.

    A row for a spec that no longer writes tells a reader to be careful about
    state that is not there, and it is how a table starts describing a suite
    that has moved on. Kept separate from the missing-rows case so a failure
    says which way the drift went.
    """
    stale = sorted(_files_named_in_the_readme() - _files_that_write())
    assert not stale, (
        "frontend/e2e/README.md's write inventory names %s, which perform no "
        "SQL write. Either the spec stopped writing and the row should go, or "
        "the write moved into a comment and the row is now describing prose"
        % stale)
