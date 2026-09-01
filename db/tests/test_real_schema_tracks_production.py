"""The three service harnesses build `applications` from the real file, not by hand.

**RF-26, and why a guard rather than a one-time cleanup.** Three service tests
each hand-wrote `CREATE TABLE applications` for their throwaway schema. Measured
against `db/init/001_schema.sql` before the fix, none matched: one was missing
`created_at`, one was missing six columns including `request_fingerprint`, and one
created five of twenty-four.

Nothing was failing because of that, which is the point. The failure mode is the
NEXT column: a migration adds one, the code under test starts writing it, and the
harness dies with `UndefinedColumn` before reaching the behaviour it exists to
check. The error then names the test's private table instead of the change, and
migrations 0038 and 0039 each cost a round of exactly that.

Cleaning them up once fixes today. The guard is what stops a fourth copy
appearing, which is the same defect with a new file name.

**What is deliberately still allowed.** A test may create a table in a shape
production does not have, when that IS the thing under test --
`test_offer_repair_real_postgres.py` builds `offers` without the constraint
migration 0026 adds, because it repairs rows that predate it. That deviation is
legitimate. What is not legitimate is an *accidental* deviation, and the
difference is whether it was written down. So this file bans hand-writing
`applications` specifically -- the table whose drift RF-26 recorded -- rather than
banning all local DDL and pretending every deviation is a bug.
"""
import pathlib
import re

import pytest

import real_schema

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]

#: The three files RF-26 named. Listed explicitly rather than globbed: a new
#: service test that hand-writes the table should be caught by the sweep below,
#: and these three should additionally be proven to use the helper.
CONVERTED = (
    "services/origination-service/tests/test_access_token_lifecycle.py",
    "services/origination-service/tests/test_decision_attempt_real_postgres.py",
    "services/disclosure-service/tests/test_offer_repair_real_postgres.py",
)

_HANDWRITTEN = re.compile(r"CREATE TABLE (?:IF NOT EXISTS )?applications\s*\(",
                          re.I)


@pytest.mark.parametrize("relpath", CONVERTED)
def test_the_converted_harness_uses_the_canonical_helper(relpath):
    text = (REPO_ROOT / relpath).read_text(encoding="utf-8")

    assert "real_schema" in text, (
        "%s no longer references the canonical schema helper" % relpath)
    assert not _HANDWRITTEN.search(text), (
        "%s hand-writes CREATE TABLE applications again. That is RF-26: the "
        "private copy drifts from db/init/001_schema.sql and the next added "
        "column fails the harness instead of the code. Use "
        "real_schema.sql_for(SCHEMA, [\"applications\"])." % relpath)


def test_no_service_test_hand_writes_the_applications_table():
    """The sweep, so a fourth copy cannot appear in a file nobody listed here."""
    offenders = []
    for path in sorted((REPO_ROOT / "services").glob("*/tests/*.py")):
        if _HANDWRITTEN.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(REPO_ROOT)).replace("\\", "/"))

    assert offenders == [], (
        "these service tests hand-write CREATE TABLE applications:\n  %s\n"
        "Build it from db/init/001_schema.sql via db/tests/real_schema.py "
        "instead -- a private copy is a schema that drifts silently."
        % "\n  ".join(offenders))


# ── the helper's own contract ────────────────────────────────────────────────

def test_the_extracted_definition_is_the_file_s_own_text():
    """Verbatim, or it is a fourth hand-written copy with extra steps."""
    canonical = (REPO_ROOT / "db" / "init" / "001_schema.sql").read_text(
        encoding="utf-8")

    for table in ("applicants", "applications", "decisions"):
        assert real_schema.definition_of(table) in canonical, (
            "the definition returned for %r is not present verbatim in "
            "001_schema.sql" % table)


def test_dependencies_match_the_references_in_the_file():
    """The declared graph is checked against the file rather than trusted.

    A wrong dependency fails as a confusing `UndefinedTable` at CREATE time, so
    the cheap thing is to assert the declaration matches the REFERENCES clauses
    the canonical file actually carries.
    """
    for table, declared in real_schema.DEPENDENCIES.items():
        # COMMENTS STRIPPED, and the table name must be followed by the column
        # list a real foreign key carries. The first version matched
        # `REFERENCES\s+(\w+)` anywhere in the definition, and `ledger_entries`
        # has a comment containing the word "REFERENCES here" -- so the guard
        # demanded a dependency on a table called `here`. A constraint parser
        # that reads prose reports defects that do not exist and, worse, would
        # miss a real FK written in a shape it does not expect.
        body = re.sub(r"--.*", "", real_schema.definition_of(table))
        referenced = {
            name for name in re.findall(r"REFERENCES\s+([A-Za-z_][A-Za-z_0-9]*)\s*\(",
                                        body)
            if name != table
        }
        missing = referenced - set(declared)
        assert not missing, (
            "%s REFERENCES %s, which real_schema.DEPENDENCIES does not declare "
            "-- creating it would fail with UndefinedTable"
            % (table, sorted(missing)))

        # BOTH DIRECTIONS. Codex review of PR #159, RF26-DEPS-EXTRA-UNCHECKED:
        # checking only `referenced - declared` accepts an EXTRA declared parent,
        # and my own first draft had one -- `loans -> offers`, guessed from the
        # name, when `loans` has no foreign keys at all in production. An extra
        # edge is not harmless: every caller asking for `loans` would silently
        # also get a canonical `offers`, which is exactly what the one harness
        # that must deviate cannot have. The graph is meant to mirror the file,
        # so it is compared to the file exactly.
        extra = set(declared) - referenced
        assert not extra, (
            "real_schema.DEPENDENCIES declares %s as a parent of %s and the "
            "canonical definition does not REFERENCE it. An invented edge drags "
            "an unrelated table into every caller's schema -- and can force a "
            "canonical shape on a harness that deliberately deviates."
            % (sorted(extra), table))


def test_resolve_orders_parents_before_children():
    order = real_schema.resolve(["decisions"])

    assert order.index("applicants") < order.index("applications")
    assert order.index("applications") < order.index("decisions")


def test_a_missing_table_fails_loudly():
    """Never silently return nothing: a test that built no table would go on to
    fail somewhere far away from the cause."""
    with pytest.raises(LookupError):
        real_schema.definition_of("table_that_does_not_exist")


def test_the_real_applications_table_has_the_columns_the_old_copies_lacked():
    """Names the specific drift RF-26 recorded, so the fix is legible.

    If the canonical table ever loses these, this test is wrong and should be
    changed deliberately -- but a silent loss is what it is here to catch.
    """
    body = real_schema.definition_of("applications")

    for column in ("created_at", "request_fingerprint", "idempotency_key",
                   "resume_token_hash"):
        assert re.search(r"\b%s\b" % column, body), (
            "%s is not in the canonical applications table" % column)
