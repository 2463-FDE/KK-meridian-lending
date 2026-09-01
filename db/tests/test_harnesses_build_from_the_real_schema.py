"""RF-26: a test harness must not keep its own copy of a production table.

The defect is not that a hand-written `CREATE TABLE` is ugly. It is that it goes
stale silently and then fails in the wrong place: a migration adds a column, the
code under test starts writing it, and the harness dies with `UndefinedColumn`
pointing at the fixture rather than at the change. Two migrations (0038, 0039)
each cost a round of exactly that, and this file's own subject matter provided a
third -- `test_reconcile_real_postgres.py` had a comment recording that
`correlation_id` had been added to its private copy by hand for that reason.

Worse than a build failure is a copy that builds and is WRONG. Moving these
harnesses onto the real definitions found one: a `payments` fixture wrote a
`name` column that production does not have, so a test asserting the cardholder
name never reaches an operator listing had been asserting it about a column that
never existed.

THIS FILE IS A SOURCE-LEVEL GUARD, deliberately. The behaviour it protects --
"the harness follows `db/init` automatically" -- cannot be demonstrated by a
passing test suite, because a stale copy passes too until the day it does not.
What can be checked is the architecture: no test builds a production table by
hand unless it says why.

A DELIBERATE DEVIATION IS STILL ALLOWED, and two exist. That is the rule
`real_schema` states in its own docstring: a deviation is fine, an accident is
the defect, and the difference is that a deviation is written down beside the
thing it deviates from.
"""
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]

#: Production tables. Anything on this list, created by a test, is either taken
#: from the real schema or explained.
_PRODUCTION_TABLES = {
    "applicants", "applications", "decisions", "kyc_checks", "decision_events",
    "manual_reviews", "decision_attempts", "offers", "loans", "balances",
    "ledger_entries", "payments", "payment_applications", "audit_logs",
    "pending_movements", "reconciliation_review_items",
    "manual_dti_assessments", "manual_dti_source_documents",
}

#: Test trees that must build from the real schema.
#:
#: `db/tests/` is deliberately NOT here. Those files test the SCHEMA -- a
#: migration's expand/contract behaviour, a constraint's edges -- and several
#: must construct a pre-migration shape on purpose, which is the one thing a
#: canonical definition cannot give them. `migration_paths.py` is their tool.
_SERVICE_TESTS = sorted((REPO / "services").glob("*/tests/test_*.py"))

_CREATE_TABLE = re.compile(
    r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?([a-z_][a-z_0-9]*)",
    re.IGNORECASE)

#: A deviation has to say so within a few lines of itself, in the file's own
#: words. Same same-clause rule the citation and reversal guards use.
_EXPLAINED = re.compile(
    r"deliberate|deliberately|on purpose|without the constraint|predate"
    r"|pre-0026|must be able to seed|not the real shape|simplified",
    re.IGNORECASE)


def _hand_written(path: pathlib.Path):
    """Production tables this file creates by hand, with the context around each.

    Context is the twelve lines before the statement: a deviation is explained
    where it is made, and a note at the top of a four-hundred-line file is not
    an explanation of something in the middle of it.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    found = []
    for i, line in enumerate(lines):
        match = _CREATE_TABLE.search(line)
        if not match:
            continue
        table = match.group(1).lower()
        if table not in _PRODUCTION_TABLES:
            continue
        context = "\n".join(lines[max(0, i - 12):i + 1])
        found.append((table, context))
    return found


def test_the_scan_finds_the_files_it_claims_to_check():
    """A glob that matched nothing would make everything below vacuous."""
    assert len(_SERVICE_TESTS) > 20, len(_SERVICE_TESTS)
    assert any("real_schema" in p.read_text(encoding="utf-8")
               for p in _SERVICE_TESTS), (
        "no service test uses the canonical helper at all, which means this "
        "guard is checking a convention nobody follows")


def test_no_service_test_hand_writes_a_production_table_without_saying_why():
    """The RF-26 invariant, as a rule rather than as three fixed filenames.

    Named files would close RF-26 for exactly those three and let the fourth
    appear under a new name, which is the failure the register asks about
    directly.
    """
    offenders = []
    for path in _SERVICE_TESTS:
        for table, context in _hand_written(path):
            if _EXPLAINED.search(context):
                continue
            offenders.append(f"{path.relative_to(REPO).as_posix()} -> {table}")

    assert offenders == [], (
        "these tests build a production table by hand with no stated reason:\n  "
        + "\n  ".join(offenders)
        + "\n\nTake it from `db/tests/real_schema.py` so it follows db/init, or "
          "write down why this one must deviate.")


def test_the_two_known_deviations_are_still_explained_where_they_are_made():
    """The allowance is not a loophole: it has to keep being paid for.

    Both surviving deviations exist for a reason a canonical definition cannot
    serve -- one seeds offers that predate `0026`'s constraint in order to
    repair them, the other seeds incomplete offers to prove the read path
    refuses them. If either explanation is deleted, the deviation stops being
    documented and this fails.
    """
    known = {
        "services/disclosure-service/tests/test_offer_repair_real_postgres.py": "offers",
        "services/origination-service/tests/test_decision_attempt_real_postgres.py": "offers",
    }
    for rel, table in known.items():
        path = REPO / rel
        assert path.is_file(), rel
        hand = [t for t, _ in _hand_written(path)]
        assert table in hand, (
            f"{rel} no longer hand-writes {table}. If it now takes the canonical "
            "definition, remove it from this list -- but check first that the "
            "constraint it needed to avoid is genuinely no longer in its way")
        for t, context in _hand_written(path):
            if t == table:
                assert _EXPLAINED.search(context), (
                    f"{rel}'s {table} deviation lost its explanation")


def test_the_helper_reads_every_init_file_that_defines_a_table():
    """The widening this PR rests on, checked rather than assumed.

    `manual_reviews` (005) and `decision_events` (004) are why harnesses had no
    choice but to hand-write them: the helper read `001_schema.sql` alone, so
    "take it from the real schema" was advice a caller could not follow.
    """
    helper = (REPO / "db" / "tests" / "real_schema.py").read_text(encoding="utf-8")
    init_dir = REPO / "db" / "init"

    defining = sorted(
        p.name for p in init_dir.glob("*.sql")
        if re.search(r"CREATE\s+TABLE", p.read_text(encoding="utf-8"), re.I))

    missing = [name for name in defining if name not in helper]
    assert missing == [], (
        "these db/init files define tables and the helper does not read them, "
        "so a harness needing one of those tables cannot take it from the real "
        "schema: %s" % missing)
