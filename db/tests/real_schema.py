"""Real table definitions, read out of the canonical schema file.

**The problem this solves (RF-26).** Three service tests need a real Postgres and
a throwaway schema, and each hand-wrote its own `CREATE TABLE applications`. None
of the three matched the real table, and the drift was not small: measured against
`db/init/001_schema.sql`, one was missing `created_at`, one was missing six columns
including `request_fingerprint`, and one created five columns out of twenty-four.

Nothing was broken by that yet, which is exactly why it is worth fixing now. The
failure mode is the NEXT column: a migration adds one, the production code under
test starts writing it, and the harness fails with `UndefinedColumn` before the
behaviour under test is reached. The error then points at the test's private table
rather than at the change, and two migrations (0038, 0039) each cost a round of
that.

**Why extraction rather than running the whole init.** `migration_paths.py`
already builds a complete database from the real files, and that is the right tool
for a test *about* the schema. These three are not: they are behaviour tests that
happen to need somewhere to put a row. Running five init files to test one
endpoint's WHERE clause is slower and drags in tables the test never touches, so
this reads the canonical file and executes only the definitions asked for --
verbatim, so the shape tracks production by construction rather than by someone
remembering.

**What this deliberately does not do.** It does not apply migrations. A caller who
needs the *migrated* shape wants `migration_paths.py`. And it does not stop a test
from deviating on purpose -- `test_offer_repair_real_postgres.py` must create
`offers` WITHOUT the constraint 0026 adds, because it exists to repair rows that
predate it. A deliberate deviation is fine; an accidental one is the defect. The
difference is that a deviation now has to be written down next to the thing it
deviates from.
"""
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
INIT_SCHEMA = REPO_ROOT / "db" / "init" / "001_schema.sql"

#: Tables each table needs before it can be created, so a caller can ask for one
#: thing and get a schema that actually builds. Kept explicit rather than derived
#: from the REFERENCES clauses: a wrong dependency graph fails confusingly, and
#: the real graph for the handful of tables these tests use is short enough to
#: state. Asserted against the file by
#: `db/tests/test_real_schema_tracks_production.py`.
DEPENDENCIES = {
    "applicants": (),
    "applications": ("applicants",),
    "decisions": ("applications",),
}


def _definition(table: str) -> str:
    """The verbatim `CREATE TABLE` statement for `table`, from the canonical file.

    Verbatim is the whole point: a paraphrase is a fourth hand-written copy with
    extra steps.
    """
    sql = INIT_SCHEMA.read_text(encoding="utf-8")
    match = re.search(
        r"CREATE TABLE (?:IF NOT EXISTS )?%s\s*\(.*?\n\);" % re.escape(table),
        sql, re.S)
    if match is None:
        raise LookupError(
            "no CREATE TABLE for %r in %s -- if the table was renamed, this "
            "helper should fail loudly rather than let a test build a shape "
            "production does not have" % (table, INIT_SCHEMA))
    return match.group(0)


def definition_of(table: str) -> str:
    """Public accessor, for tests that want to assert on the text itself."""
    return _definition(table)


def resolve(tables) -> list:
    """`tables` plus everything they depend on, in creatable order."""
    ordered = []

    def visit(name):
        if name in ordered:
            return
        for parent in DEPENDENCIES.get(name, ()):
            visit(parent)
        ordered.append(name)

    for table in tables:
        visit(table)
    return ordered


def create(cursor, schema: str, tables) -> list:
    """Create `tables` (and their dependencies) inside `schema`, real shape.

    The caller owns the schema and the transaction. Returns the order used, so a
    test can assert what it got rather than assume.
    """
    created = resolve(tables)
    cursor.execute("SET search_path TO %s" % schema)
    for table in created:
        cursor.execute(_definition(table))
    return created


def sql_for(schema: str, tables) -> str:
    """The same thing as one script, for callers that execute a single string."""
    parts = ["SET search_path TO %s;" % schema]
    parts.extend(_definition(t) for t in resolve(tables))
    return "\n".join(parts)
