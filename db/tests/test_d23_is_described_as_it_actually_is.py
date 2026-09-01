"""D23 is half-built, and the register has to say so in both directions.

D23 is the entry most likely to be read wrong, because it is the one where the
truth has two halves that point opposite ways:

  * the PRIMITIVE exists -- `ledger_entries.installment_no`, a partial unique
    index enforcing one fee per installment, a trigger validating the
    installment against the loan's own schedule, and `late_fee_for_installment()`
    implementing the decided arithmetic (PR #143);
  * the RUNTIME has not moved -- `assess_late_fee` still reads
    `balances.past_due` and prices with `late_fee_for()`, the superseded rule.

Either half stated alone is a lie, and the register has already told the first
one. Before this file existed, D23 read "IMPLEMENTATION REQUIRES DATA-MODEL
EXPANSION" and asserted as present-tense fact that nothing records which
installment a fee belongs to and that no `installment`, `period` or `due_date`
column exists anywhere under `db/`. That was true when written and had been
false since #143 merged.

THE TWO LIES THIS PINS

  LIE A -- "the installment primitive does not exist", while `0046` and the
  schema carry `installment_no`. A reader believes a migration is still needed
  and re-plans work that is merged.

  LIE B -- "D23 is done", while a borrower is still charged under the arrears
  rule the client replaced. That one is worse: it closes a row over live
  incorrect behaviour.

WHAT IT DELIBERATELY DOES NOT DO. It does not freeze a paragraph. Wording that
has to be matched exactly fails on every honest edit and gets deleted, taking
the guard with it. It reads the CODE for each half and then asks whether the
register's account of that half is compatible with it.
"""
import ast
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
DEBT = REPO / "docs" / "DEBT.md"
SCHEMA = REPO / "db" / "init" / "001_schema.sql"
MIGRATION = REPO / "db" / "migrations" / "0046_ledger_installment_no.sql"
DELINQUENCY = (REPO / "services" / "servicing-service" / "app" / "delinquency.py")
INSTALLMENTS = (REPO / "services" / "servicing-service" / "app" / "installments.py")


def _d23_row() -> str:
    """The D23 row, whitespace-normalised.

    Normalised because the row is one enormous line whose prose wraps only in an
    editor -- but a future edit that splits it would otherwise change what the
    patterns below can match, which is a fragility unrelated to what is being
    checked.
    """
    for line in DEBT.read_text(encoding="utf-8").splitlines():
        if line.startswith("| **D23**"):
            return " ".join(line.split())
    raise AssertionError(
        "no D23 row in docs/DEBT.md. If the row was renamed or removed, this "
        "guard must fail rather than pass by finding nothing to check")


# --------------------------------------------------------------------------
# The two facts, read from the code rather than from the register.
# --------------------------------------------------------------------------

def _primitive_exists() -> bool:
    """Whether an installment can be recorded against a ledger entry at all.

    Read from the fresh-init schema AND the migration, because the two must
    agree -- a column present in only one of them is a different defect that
    `db/tests/test_migration_paths.py` owns, and this guard should not pass by
    finding it in whichever file happens to have it.
    """
    return ("installment_no" in SCHEMA.read_text(encoding="utf-8")
            and "installment_no" in MIGRATION.read_text(encoding="utf-8"))


def _runtime_still_uses_arrears() -> bool:
    """Whether the money path still prices the superseded way.

    `assess_late_fee` is the function that writes the fee. It is arrears-priced
    while it reads `balances.past_due` and calls `late_fee_for(...)`; it has cut
    over when it prices from `late_fee_for_installment(...)` instead. Both
    markers are checked inside that function's own body, not file-wide --
    `late_fee_for_installment` is DEFINED in this file, so a file-wide search
    would report the cutover as done the moment the arithmetic was written.
    """
    tree = ast.parse(DELINQUENCY.read_text(encoding="utf-8"))
    fn = next((n for n in ast.walk(tree)
               if isinstance(n, ast.FunctionDef) and n.name == "assess_late_fee"),
              None)
    assert fn is not None, (
        "assess_late_fee no longer exists in delinquency.py -- this guard must "
        "fail rather than decide the runtime cut over because it found nothing")

    # CALLS, from the AST, with the docstring excluded. A text scan of the
    # function reported the cutover as DONE the moment the docstring started
    # explaining that `late_fee_for_installment()` exists and is not used --
    # prose about a function is not a call to it.
    called = {n.func.id for n in ast.walk(fn)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    return "late_fee_for" in called and "late_fee_for_installment" not in called


# --------------------------------------------------------------------------
# LIE A -- claiming the primitive is missing.
# --------------------------------------------------------------------------

#: The row is one line; its structure is `<br>`-separated blocks of prose, and
#: within those, sentences. Both are split so a retraction two thousand
#: characters away cannot excuse an assertion here -- the same same-clause rule
#: the citation and reversal guards use, for the same reason.
_CLAUSE_SPLIT = re.compile(r"<br>|(?<=[.;])" + chr(92) + "s+")


def _clauses(row: str) -> list:
    return [c.strip() for c in _CLAUSE_SPLIT.split(row) if c.strip()]


#: What marks a clause as an account of what the row USED to say, rather than a
#: claim about what is true now.
_HISTORICAL = re.compile(
    r"previously read|used to|stopped being true|no longer true"
    r"|was true when|this column previously|had been false|before #143",
    re.IGNORECASE)


#: Ways of saying "there is no installment representation". Each is a claim the
#: row actually made before #143, kept as a pattern rather than a literal so a
#: reworded version of the same falsehood is caught too.
_DENIES_THE_PRIMITIVE = (
    re.compile(r"nothing records which installment", re.I),
    re.compile(r"no `?installment[_ ]?(no|number)?`?[^.]{0,40}column exists", re.I),
    re.compile(r"IMPLEMENTATION REQUIRES DATA-MODEL EXPANSION", re.I),
    re.compile(r"installment.{0,30}NOT REPRESENTED", re.I),
)


def test_d23_does_not_claim_the_installment_primitive_is_missing():
    """LIE A. Fails against the row exactly as it read before this correction."""
    if not _primitive_exists():                            # pragma: no cover
        pytest.skip("the installment primitive is genuinely absent")

    offenders = []
    for clause in _clauses(_d23_row()):
        if _HISTORICAL.search(clause):
            # The row QUOTES its own retired wording in order to retract it --
            # "this column previously read ... and stated, as present-tense
            # fact, that nothing records which installment a fee belongs to".
            # Banning the words would force that history out of the register and
            # leave a reader unable to see what changed, so what is forbidden is
            # the claim standing as an ASSERTION, clause by clause.
            continue
        offenders.extend(p.pattern for p in _DENIES_THE_PRIMITIVE
                         if p.search(clause))
    assert offenders == [], (
        "D23 still says the installment primitive does not exist, and it does: "
        "`ledger_entries.installment_no` is in db/migrations/0046 and in "
        "db/init/001_schema.sql. A reader believes a migration is still needed "
        "and re-plans merged work. Offending patterns: %s" % offenders)


def test_d23_names_what_was_actually_built():
    """The other side of LIE A: silence is its own inaccuracy.

    A row that merely stops denying the primitive, without saying it landed,
    leaves a reader with no way to tell a half-built entry from an unstarted
    one. These are the load-bearing artefacts of #143, and each is named because
    a reader following D23 should be able to open it.
    """
    if not _primitive_exists():                            # pragma: no cover
        pytest.skip("the installment primitive is genuinely absent")

    row = _d23_row()
    for artefact in ("installment_no",
                     "ledger_one_late_fee_per_installment",
                     "late_fee_for_installment",
                     "installments.py"):
        assert artefact in row, (
            f"D23 does not mention {artefact!r}, which PR #143 added. The row "
            "cannot be read as partially implemented if it never says what the "
            "implemented part is")


# --------------------------------------------------------------------------
# LIE B -- claiming the work is finished while a borrower is still charged
# under the superseded rule.
# --------------------------------------------------------------------------

#: Markers that would present D23 as complete. Deliberately anchored on the
#: STATUS vocabulary this register uses ("Fixed", "Done", "Closed") rather than
#: on any sentence, and required at the start of the status column, because the
#: row legitimately contains the word "implemented" many times while describing
#: which parts are and are not.
_CLAIMS_DONE = re.compile(
    r"\|\s*\*\*(FIXED|DONE|CLOSED|RESOLVED|COMPLETE)\b", re.I)


def test_d23_is_not_marked_done_while_the_runtime_still_prices_off_arrears():
    """LIE B, and the more expensive of the two.

    `assess_late_fee` still reads `balances.past_due` and prices with
    `late_fee_for()`. A borrower is charged under the rule the client replaced
    on 2026-08-29. Marking the row Fixed would close a register entry over live
    incorrect behaviour, which is the one thing a debt register must never do.
    """
    if not _runtime_still_uses_arrears():                  # pragma: no cover
        pytest.skip("the runtime has cut over; this guard's premise is gone")

    row = _d23_row()
    assert not _CLAIMS_DONE.search(row), (
        "D23's status opens as fixed/done while assess_late_fee still prices "
        "off balances.past_due with late_fee_for(). The row may say the "
        "PRIMITIVE is implemented; it may not say the entry is closed")

    assert re.search(r"PARTIAL|PARTIALLY", row, re.I), (
        "D23 is half-built and its status does not say so. A reader scanning "
        "statuses should be able to tell this from the marker alone")


def test_d23_still_describes_the_runtime_it_actually_has():
    """The runtime half must be stated, not left to inference.

    Naming `assess_late_fee` and `past_due` is what makes the open half
    checkable: a reader can open that function and see the arrears rule for
    themselves.
    """
    if not _runtime_still_uses_arrears():                  # pragma: no cover
        pytest.skip("the runtime has cut over")

    row = _d23_row()
    assert "assess_late_fee" in row and "past_due" in row, (
        "D23 does not name the function that still prices off arrears, so a "
        "reader cannot check the open half against the code")


# --------------------------------------------------------------------------
# The blockers, which are the reason the runtime has not moved.
# --------------------------------------------------------------------------

def test_d23_names_both_client_blockers_and_neither_is_invented():
    """Two answers are missing, and the row must say which two.

    A row that says "blocked" without naming what would unblock it is an
    excuse. And the guard checks the CODE side of both: `grace_days` is a
    required argument precisely so nobody defaults it, and
    `unpaid_scheduled_pi` raises rather than picking an allocation order.
    """
    row = _d23_row()
    assert re.search(r"grace period", row, re.I), "D23 does not name the grace-period blocker"
    assert re.search(r"allocation order|attribution", row, re.I), (
        "D23 does not name the payment-to-installment blocker")

    installments = INSTALLMENTS.read_text(encoding="utf-8")
    assert "InstallmentAttributionUnknown" in installments, (
        "installments.py no longer refuses to attribute payments, so either the "
        "blocker was answered -- and D23 must say so -- or an allocation order "
        "was invented")
    assert re.search(r"def overdue_installments\([^)]*grace_days(?!\s*[:=][^,)]*=)",
                     installments, re.S), (
        "overdue_installments no longer takes grace_days as a required "
        "argument. A default here would be a grace period nobody decided")


def test_the_migration_backfilled_nothing():
    """The claim "no historical fake backfill" is checked, not repeated.

    Every fee assessed before #143 was assessed under the arrears rule and
    belongs to no installment. Labelling those rows would manufacture a fact
    nobody recorded -- so the migration must write no `installment_no` onto
    existing data, and D23 must not claim it did.
    """
    migration = MIGRATION.read_text(encoding="utf-8")
    assert not re.search(r"UPDATE\s+ledger_entries\s+SET[^;]*installment_no",
                         migration, re.I | re.S), (
        "0046 backfills installment_no onto existing ledger rows")
