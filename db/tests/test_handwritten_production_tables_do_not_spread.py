"""RF-26's subject, counted by machine instead of by a list in a sentence.

**WHY THE ROW NEEDED THIS.** RF-26 read, in the present tense:

    Three test files hand-write their own `CREATE TABLE applications` instead of
    building from `db/init/001_schema.sql`: `test_access_token_lifecycle.py`,
    `test_decision_attempt_real_postgres.py` and `test_offer_repair_real_postgres.py`.

Both halves had stopped being true. PR #159 converted all three -- none of them
now hand-writes `applications` at all -- so the register described as open
something that was closed. And the count was never three: sweeping the whole test
tree for a hand-written production-shaped table finds twelve more files, none of
which the row mentioned, all in `db/tests`.

That is RF-26's own defect wearing the costume it warns about. The row diagnosed
"a hand-maintained list reads complete while missing an entry" and then WAS one.
Five defects in this repository have had that shape, which is why the count here
is derived from the tree on every run rather than written down.

**WHAT THIS ASSERTS, and what it deliberately does not.** It does not fail on the
twelve. They are pre-existing, they pass, and converting them is a real change to
concurrency and lifecycle harnesses -- not something to do inside a fixture PR.
It is a RATCHET: the set may shrink and must not grow. A new test that hand-writes
a production shape fails here, with `real_schema.py` named as the alternative.

**Three kinds of hand-written table are excluded, each for a stated reason:**

*Schema tooling.* `migration_paths.py`, `test_schema_parity.py` and
`test_real_schema_tracks_production.py` build tables from the real files as their
whole purpose, or hand-write one as a negative control for the helper itself.

*Pre-migration shapes.* A `test_00NN_*.py` file that creates the OLD shape and
applies a migration to it is doing the only thing it can: the point is the
migration, and building the CURRENT shape would test nothing. These are
deliberate and are counted separately so the exclusion is visible rather than
silent.

*Documented deviations.* A file that uses `real_schema` and also hand-writes one
table has made a choice the helper's own docstring anticipates --
`test_offer_repair_real_postgres.py` must create `offers` WITHOUT the constraint
0026 adds, because it exists to repair rows that predate it. A deliberate
deviation is fine; an accidental one is the defect.
"""
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]

#: The tables whose shape production owns. A hand-written copy of one of these
#: is what goes stale when a column is added; a table a test invents for itself
#: (`unrelated_balance_source`, `listing`) is not production's to track.
PRODUCTION_TABLES = (
    "applicants", "applications", "decisions", "decision_events",
    "decision_attempts", "manual_reviews", "offers", "loans", "balances",
    "ledger_entries", "payments", "payment_applications", "pending_movements",
    "audit_logs", "reconciliation_runs", "reconciliation_review_items",
    "manual_dti_assessments", "manual_dti_source_documents", "kyc_checks",
)

#: Files whose job IS to build or compare schemas. See the module docstring.
SCHEMA_TOOLING = {
    "migration_paths.py",
    "test_schema_parity.py",
    "test_migration_paths_converge.py",
    "test_real_schema_tracks_production.py",
    pathlib.Path(__file__).name,
}

#: The measured count on 2026-09-02, outside every exclusion above. It may go
#: DOWN. If it goes up, a new hand-written copy was added and this fails.
KNOWN_HANDWRITTEN = 12

_CREATE = re.compile(
    r"CREATE TABLE\s+(?:IF NOT EXISTS\s+)?(%s)\b" % "|".join(PRODUCTION_TABLES),
    re.I)


def _test_files():
    files = sorted((REPO / "db" / "tests").glob("*.py"))
    for service in sorted((REPO / "services").glob("*")):
        tests = service / "tests"
        if tests.is_dir():
            files.extend(sorted(tests.glob("*.py")))
    return files


def _classify():
    """Every test file that hand-writes a production table, by kind."""
    tooling, pre_migration, deviations, plain = [], [], [], []
    for path in _test_files():
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:                                    # pragma: no cover
            continue
        if not _CREATE.search(text):
            continue
        name = path.name
        if name in SCHEMA_TOOLING:
            tooling.append(name)
        elif re.match(r"test_\d{4}_", name):
            pre_migration.append(name)
        elif "real_schema" in text:
            deviations.append(name)
        else:
            plain.append(name)
    return tooling, pre_migration, deviations, plain


def test_the_sweep_finds_something_to_classify():
    """Guard the guard.

    If `PRODUCTION_TABLES` or the pattern stopped matching, every count below
    would be zero and the ratchet would read as total success.
    """
    tooling, pre_migration, deviations, plain = _classify()
    total = len(tooling) + len(pre_migration) + len(deviations) + len(plain)
    assert total >= 10, (
        "the sweep found only %d test files creating a production table. The "
        "pattern or the table list has stopped matching, so the ratchet below "
        "would pass by checking nothing" % total)


def test_hand_written_production_tables_have_not_spread():
    """The ratchet. May shrink; must not grow."""
    _tooling, _pre, _dev, plain = _classify()
    assert len(plain) <= KNOWN_HANDWRITTEN, (
        "%d test files hand-write a production-shaped table, up from the %d "
        "measured for RF-26. A new one was added: build it from "
        "`db/tests/real_schema.py` instead, which takes the definition verbatim "
        "from `db/init` so the next column added reaches the test by "
        "construction.\n%s"
        % (len(plain), KNOWN_HANDWRITTEN, "\n".join("  " + n for n in plain)))


def test_the_row_no_longer_names_three_converted_files_as_open():
    """The claim half. RF-26 named three files that #159 converted.

    Read from the tree rather than trusted: if any of the three starts
    hand-writing `applications` again this fails, and so does a register row
    that goes back to calling them open.
    """
    converted = (
        REPO / "services" / "origination-service" / "tests" / "test_access_token_lifecycle.py",
        REPO / "services" / "origination-service" / "tests" / "test_decision_attempt_real_postgres.py",
        REPO / "services" / "disclosure-service" / "tests" / "test_offer_repair_real_postgres.py",
    )
    for path in converted:
        assert path.is_file(), "RF-26 cites %s, which does not exist" % path.name
        text = path.read_text(encoding="utf-8")
        assert "real_schema" in text, (
            "%s no longer builds from db/tests/real_schema.py, so RF-26's three "
            "named files are not all converted after all" % path.name)
        assert not re.search(
            r"CREATE TABLE\s+(?:IF NOT EXISTS\s+)?applications\b", text, re.I), (
            "%s hand-writes `applications` again -- the exact defect RF-26 "
            "recorded and #159 closed" % path.name)


def test_the_register_states_the_derived_count_rather_than_a_list():
    """RF-26 must say what is actually left, and say it is counted.

    The row's original failure was a sentence naming three files. A corrected
    sentence naming twelve would fail the same way in a month, so the row has to
    point at this file.
    """
    row = ""
    debt = (REPO / "docs" / "DEBT.md").read_text(encoding="utf-8")
    for line in debt.splitlines():
        if line.startswith("| **RF-26**"):
            row = " ".join(line.split())
            break
    assert row, "no RF-26 row in docs/DEBT.md"
    assert pathlib.Path(__file__).name in row, (
        "RF-26 does not cite %s, so its count is maintained by remembering -- "
        "which is the defect the row is about" % pathlib.Path(__file__).name)
    assert str(KNOWN_HANDWRITTEN) in row, (
        "RF-26 does not state the measured count (%d) this file ratchets"
        % KNOWN_HANDWRITTEN)
