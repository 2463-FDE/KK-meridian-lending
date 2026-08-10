"""Migration 0031 must refuse to run unless the contract preconditions hold.

Reviewed as high severity: the migration runner applies every `*.sql` in
filename order, so a deploy carrying both the expand migration and this drop
would remove `payments.pan` in the same breath as the back-fill that made
losing it survivable. Any servicing instance still running the previous image
reads `payment.pan` and starts failing the moment the ALTER commits.

The separation between the two releases previously existed only in branch
topology (this PR is based on PR #11's branch, not on main) and in prose. A
merge to `main` erases both. These tests assert that the ordering is now
enforced by the migration itself, where it cannot be skipped by someone who did
not read the runbook.

Two independent preconditions, tested separately because they fail differently
and one can hold while the other does not:

  1. the back-fill is complete -- no row holds a `pan` with a NULL `last4`;
  2. the operator has acknowledged the deploy check, via a session GUC.

The negative cases matter more than the positive one here. A gate that lets the
migration through is indistinguishable from no gate at all, and the whole
finding was that the drop could happen too early.
"""
import os
import pathlib

import psycopg2
import pytest

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against")

MIGRATIONS = pathlib.Path(__file__).resolve().parents[1] / "migrations"
REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
SCHEMA = "contract_gate_0031"

_0031 = (MIGRATIONS / "0031_drop_payments_pan_cvv.sql").read_text()

# Just enough of `payments` for the gate to have something to inspect. Built by
# hand rather than from db/init so the test states exactly which columns the
# gate depends on.
_SETUP = f"""
DROP SCHEMA IF EXISTS {SCHEMA} CASCADE;
CREATE SCHEMA {SCHEMA};
SET search_path TO {SCHEMA};
CREATE TABLE payments (
    id SERIAL PRIMARY KEY,
    loan_id INTEGER,
    pan TEXT,
    cvv TEXT,
    last4 TEXT,
    amount NUMERIC(14,2),
    method TEXT
);
"""


@pytest.fixture
def conn():
    connection = psycopg2.connect(DATABASE_URL)
    connection.autocommit = True
    with connection.cursor() as cur:
        cur.execute(_SETUP)
    yield connection
    with connection.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    connection.close()


def _run_0031(conn, *, acknowledged: bool):
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        if acknowledged:
            cur.execute("SET meridian.pan_drop_acknowledged = 'yes'")
        cur.execute(_0031)


def _columns(conn) -> set[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = 'payments'",
            (SCHEMA,),
        )
        return {r[0] for r in cur.fetchall()}


def _insert(conn, **row):
    cols = ", ".join(row)
    marks = ", ".join(["%s"] * len(row))
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(f"INSERT INTO payments ({cols}) VALUES ({marks})", tuple(row.values()))


def test_without_the_acknowledgement_the_migration_refuses(conn):
    """The default path. An operator who runs the migrations folder in order,
    with no ceremony, must not drop these columns."""
    _insert(conn, loan_id=1, pan="4111111111111111", cvv="123", last4="1111")

    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _run_0031(conn, acknowledged=False)

    assert "0031 refused" in str(exc.value)
    # And nothing was changed -- a gate that raises after the ALTER is no gate.
    assert {"pan", "cvv"} <= _columns(conn)


def test_an_incomplete_backfill_blocks_the_drop_even_when_acknowledged(conn):
    """The acknowledgement is about DEPLOYED CODE. It says nothing about
    whether the data migration finished, so it must not be able to wave that
    through: a row holding a pan with no last4 would lose the only record of
    the card used."""
    _insert(conn, loan_id=1, pan="4111111111111111", cvv="123", last4=None)

    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _run_0031(conn, acknowledged=True)

    assert "0031 refused" in str(exc.value)
    assert "back-fill" in str(exc.value)
    assert {"pan", "cvv"} <= _columns(conn)


def test_the_error_names_what_to_do_next(conn):
    """An operator hitting this at 3am needs the next command, not a diagnosis.
    Asserted so a future edit cannot quietly reduce it to 'permission denied'."""
    _insert(conn, loan_id=1, pan="4111111111111111", last4="1111")

    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _run_0031(conn, acknowledged=False)

    message = str(exc.value)
    assert "check_no_pan_readers" in message
    assert "meridian.pan_drop_acknowledged" in message
    assert "RUNBOOK" in message


def test_with_both_preconditions_satisfied_the_columns_are_dropped(conn):
    """The gate must not be so strict that the migration can never run."""
    _insert(conn, loan_id=1, pan="4111111111111111", cvv="123", last4="1111")

    _run_0031(conn, acknowledged=True)

    remaining = _columns(conn)
    assert "pan" not in remaining
    assert "cvv" not in remaining
    assert "last4" in remaining, "the surviving identifier must not be dropped too"


def test_a_database_with_no_legacy_rows_still_needs_the_acknowledgement(conn):
    """An empty table makes the back-fill check vacuous. The deploy risk is
    unchanged -- old instances still SELECT the column whether or not any row
    has a value in it -- so the acknowledgement must still be required."""
    with pytest.raises(psycopg2.errors.RaiseException) as exc:
        _run_0031(conn, acknowledged=False)
    assert "0031 refused" in str(exc.value)
    assert {"pan", "cvv"} <= _columns(conn)


def test_the_migration_is_idempotent_once_it_has_run(conn):
    """Replay safety. After a successful drop the columns are gone, and running
    it again must be a no-op rather than an error -- the runner may replay."""
    _insert(conn, loan_id=1, pan="4111111111111111", last4="1111")
    _run_0031(conn, acknowledged=True)
    _run_0031(conn, acknowledged=True)
    assert "pan" not in _columns(conn)


# --- the checker whose green result authorises the drop ----------------------

def _run_checker_over(tmp_path, source: str):
    """Run check_no_pan_readers' scan against one synthetic service file."""
    import importlib.util

    tool = REPO_ROOT / "db" / "tools" / "check_no_pan_readers.py"
    spec = importlib.util.spec_from_file_location("check_no_pan_readers", tool)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    svc = tmp_path / "services" / "fake-service" / "app"
    svc.mkdir(parents=True, exist_ok=True)
    # Unique per call so one test can check several sources.
    name = f"reader_{len(list(svc.glob('reader_*.py')))}.py"
    for stale in svc.glob("reader_*.py"):
        stale.unlink()
    (svc / name).write_text(source, encoding="utf-8")
    # Both roots, or scan() reports paths relative to the real repository and
    # raises on a tmp_path that is not under it.
    mod.SERVICES = tmp_path / "services"
    mod.REPO_ROOT = tmp_path
    return mod.scan()


def test_a_projection_split_across_lines_is_detected(tmp_path):
    """The form raw SQL is actually written in here.

    Adjacent string literals put the keyword and the column on different lines,
    so a same-line check saw neither -- and the checker printed OK over a live
    reader. That green result is the runbook's prerequisite for acknowledging a
    destructive migration. Reviewed on PR #15.
    """
    hits = _run_checker_over(tmp_path, (
        "def read(conn):\n"
        "    return conn.query(\n"
        '        "SELECT id, "\n'
        '        "pan, "\n'
        '        "last4 FROM payments"\n'
        "    )\n"
    ))
    assert hits, "a multiline projection reading pan was reported clean"
    assert any("pan" in h[3] for h in hits)


def test_an_uppercase_column_is_detected(tmp_path):
    """PostgreSQL folds an unquoted identifier, so `PAN` reads the same column."""
    hits = _run_checker_over(tmp_path, (
        "def read(conn):\n"
        '    return conn.query("SELECT PAN, CVV FROM payments")\n'
    ))
    assert hits, "an uppercase pan/cvv read was reported clean"


def test_an_unrelated_word_far_from_any_sql_is_not_a_hit(tmp_path):
    """The window is a statement body, not a licence to flag the word anywhere."""
    hits = _run_checker_over(tmp_path, (
        'def helper():\n'
        '    return conn.query("SELECT id FROM payments")\n'
        "\n"
        "\n"
        "\n"
        "\n"
        "\n"
        "\n"
        "\n"
        "def pan_display_name():\n"
        "    return 'pan'\n"
    ))
    assert hits == [], f"unexpected hits: {hits}"


# --- the gate and the ALTER must resolve the SAME table ----------------------

_EMPTY_SCHEMA = "contract_gate_0031_empty"


def test_the_gate_holds_when_an_earlier_schema_lacks_the_table(conn):
    """An ordinary search_path defeated the gate entirely.

    `current_schema()` is the FIRST schema on the path; an unqualified
    `payments` resolves to the first schema that CONTAINS it. With a path like
    `"$user", public` and an existing per-user schema, the gate looked in the
    empty schema, found no `pan`, and returned satisfied -- while the ALTER went
    on to resolve the real table and drop its columns with neither the back-fill
    check nor the acknowledgement run. Data destroyed by a migration that
    reported it had nothing to do. Reviewed on PR #15.

    The empty schema is placed FIRST here, exactly as a per-user schema sits
    ahead of public.
    """
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {_EMPTY_SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {_EMPTY_SCHEMA}")
        # A legacy row that has NOT been back-filled: the gate must refuse.
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("INSERT INTO payments (loan_id, pan, cvv, last4, amount, method) "
                    "VALUES (1, '4111111111111111', '123', NULL, 10.00, 'card')")
        cur.execute(f"SET search_path TO {_EMPTY_SCHEMA}, {SCHEMA}")
        cur.execute("SET meridian.pan_drop_acknowledged = 'yes'")

        with pytest.raises(psycopg2.errors.RaiseException) as exc:
            cur.execute(_0031)
        assert "0029 back-fill has not completed" in str(exc.value)

    # And the columns are still there: nothing was dropped behind the gate.
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = 'payments' "
                    "AND column_name IN ('pan','cvv')", (SCHEMA,))
        assert cur.fetchone()[0] == 2, "the drop ran despite an unsatisfied gate"
        cur.execute(f"DROP SCHEMA IF EXISTS {_EMPTY_SCHEMA} CASCADE")


def test_the_drop_targets_the_same_table_the_gate_checked(conn):
    """The satisfied path, through the same shadowing search_path."""
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {_EMPTY_SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {_EMPTY_SCHEMA}")
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("INSERT INTO payments (loan_id, pan, cvv, last4, amount, method) "
                    "VALUES (1, '4111111111111111', '123', '1111', 10.00, 'card')")
        cur.execute(f"SET search_path TO {_EMPTY_SCHEMA}, {SCHEMA}")
        cur.execute("SET meridian.pan_drop_acknowledged = 'yes'")
        cur.execute(_0031)

        cur.execute("SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = 'payments' "
                    "AND column_name IN ('pan','cvv')", (SCHEMA,))
        assert cur.fetchone()[0] == 0, "the columns were not dropped from the real table"
        cur.execute(f"DROP SCHEMA IF EXISTS {_EMPTY_SCHEMA} CASCADE")


def test_a_triple_quoted_query_is_scanned_not_skipped(tmp_path):
    """A multiline query is code, however it is quoted.

    Tracking triple-quote fences treated `conn.query(\"\"\"SELECT ... pan ...\"\"\")`
    as a docstring and skipped every line of it, so the checker returned exit 0
    over a live reader -- and that green result is the runbook's prerequisite
    for acknowledging the drop. Reviewed on PR #15.
    """
    hits = _run_checker_over(tmp_path, (
        "def read(conn):\n"
        '    return conn.query("""\n'
        "        SELECT id, pan, last4\n"
        "          FROM payments\n"
        '    """)\n'
    ))
    assert hits, "a triple-quoted query reading pan was reported clean"
    assert any("pan" in h[3] for h in hits)


def test_a_triple_quoted_module_constant_is_scanned(tmp_path):
    """The same string assigned to a name rather than passed inline."""
    hits = _run_checker_over(tmp_path, (
        'LEGACY_SQL = """\n'
        "    SELECT pan FROM payments WHERE id = %s\n"
        '"""\n'
    ))
    assert hits, "a triple-quoted SQL constant reading pan was reported clean"


def test_an_uppercase_triple_quoted_query_is_scanned(tmp_path):
    """PostgreSQL folds the identifier, so case must not decide this."""
    hits = _run_checker_over(tmp_path, (
        "def read(conn):\n"
        '    return conn.query("""SELECT ID, PAN FROM PAYMENTS""")\n'
    ))
    assert hits, "an uppercase triple-quoted read was reported clean"


def test_a_real_docstring_mentioning_pan_is_not_a_hit(tmp_path):
    """Prose is excluded by WHERE it sits, not by how it is capitalised.

    This codebase documents the PAN/CVV defect at length; counting those
    sentences would bury the real hits and train people to ignore the checker.
    """
    hits = _run_checker_over(tmp_path, (
        '"""This module used to SELECT pan from payments.\n'
        "\n"
        "It no longer does; see docs/DEBT.md D5b for the history of pan and cvv.\n"
        '"""\n'
        "\n"
        "def read(conn):\n"
        '    """Return the masked card for display.\n'
        "\n"
        "    Never selects pan or cvv -- last4 only.\n"
        '    """\n'
        '    return conn.query("SELECT last4, brand FROM payments")\n'
    ))
    assert hits == [], f"documentation was reported as a live read: {hits}"


def test_clean_sql_passes(tmp_path):
    """The other half: a compliant reader must not be flagged."""
    hits = _run_checker_over(tmp_path, (
        "def read(conn):\n"
        '    return conn.query("""\n'
        "        SELECT id, last4, brand, amount\n"
        "          FROM payments\n"
        "         WHERE loan_id = %s\n"
        '    """)\n'
    ))
    assert hits == [], f"clean SQL was flagged: {hits}"


# --- a partially cleaned database ---------------------------------------------

def test_a_leftover_cvv_still_requires_the_acknowledgement(conn):
    """`pan` gone, `cvv` still there: the gate must still gate.

    Both gates tested `pan` alone, so this state reported "nothing to do" and
    returned -- and the ALTER below then dropped `cvv` with no acknowledgement
    and no operator sign-off. A partially cleaned database is exactly where a
    destructive migration needs its brakes most. Reviewed on PR #15.
    """
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("ALTER TABLE payments DROP COLUMN pan")
        cur.execute("INSERT INTO payments (loan_id, cvv, last4, amount, method) "
                    "VALUES (1, '123', '1111', 10.00, 'card')")

        with pytest.raises(psycopg2.errors.RaiseException) as exc:
            cur.execute(_0031)
        assert "acknowledged" in str(exc.value).lower() or "refused" in str(exc.value).lower()

        cur.execute("SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = 'payments' "
                    "AND column_name = 'cvv'", (SCHEMA,))
        assert cur.fetchone()[0] == 1, "cvv was dropped without the acknowledgement"


def test_a_leftover_cvv_drops_once_acknowledged(conn):
    """...and with the acknowledgement it completes, without touching pan.

    The back-fill question only exists while `pan` does -- with it already gone
    there is nothing left to lose, and querying it would fail outright.
    """
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("ALTER TABLE payments DROP COLUMN pan")
        cur.execute("INSERT INTO payments (loan_id, cvv, last4, amount, method) "
                    "VALUES (1, '123', '1111', 10.00, 'card')")
        cur.execute("SET meridian.pan_drop_acknowledged = 'yes'")
        cur.execute(_0031)

        cur.execute("SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = 'payments' "
                    "AND column_name IN ('pan','cvv')", (SCHEMA,))
        assert cur.fetchone()[0] == 0


def test_a_leftover_pan_still_requires_the_acknowledgement(conn):
    """The mirror case: `cvv` gone, `pan` still present and unbacked-filled."""
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("ALTER TABLE payments DROP COLUMN cvv")
        cur.execute("INSERT INTO payments (loan_id, pan, last4, amount, method) "
                    "VALUES (1, '4111111111111111', NULL, 10.00, 'card')")

        with pytest.raises(psycopg2.errors.RaiseException) as exc:
            cur.execute(_0031)
        assert "back-fill has not completed" in str(exc.value)

        cur.execute("SELECT count(*) FROM information_schema.columns "
                    "WHERE table_schema = %s AND table_name = 'payments' "
                    "AND column_name = 'pan'", (SCHEMA,))
        assert cur.fetchone()[0] == 1, "pan was dropped despite an incomplete back-fill"


def test_a_column_far_down_a_long_projection_is_detected(tmp_path):
    """No line-distance limit: the literal is the unit.

    A fixed six-line window still missed a projection with seven fields before
    `pan`. A string literal has a real beginning and end, so there is nothing
    left to guess. Reviewed on PR #15.
    """
    hits = _run_checker_over(tmp_path, (
        "def read(conn):\n"
        '    return conn.query("""\n'
        "        SELECT id,\n"
        "               loan_id,\n"
        "               amount,\n"
        "               method,\n"
        "               brand,\n"
        "               last4,\n"
        "               created_at,\n"
        "               auth_status,\n"
        "               idempotency_key,\n"
        "               pan\n"
        "          FROM payments\n"
        '    """)\n'
    ))
    assert hits, "a column ten lines below SELECT was reported clean"


def test_adjacent_literals_are_each_scanned(tmp_path):
    """Implicit concatenation is several literals; each is checked on its own."""
    hits = _run_checker_over(tmp_path, (
        "def read(conn):\n"
        '    return conn.query(\n'
        '        "SELECT id, "\n'
        '        "pan, "\n'
        '        "last4 FROM payments"\n'
        "    )\n"
    ))
    assert hits, "a projection split across adjacent literals was reported clean"


@pytest.mark.parametrize("read", [
    'row["pan"]',
    "row['cvv']",
    'record.get("pan")',
])
def test_a_mapping_read_of_a_legacy_column_is_detected(tmp_path, read):
    """A raw query consumed as a mapping is a live read.

    `row["pan"]` carries no SQL keyword of its own -- the SELECT that produced
    the row is elsewhere, often in another function -- and the dotted-attribute
    patterns did not match it either, so it passed clean. Reviewed on PR #15.
    """
    hits = _run_checker_over(tmp_path, (
        "def display(row):\n"
        f"    return {read}\n"
    ))
    assert hits, f"a live mapping read {read} was reported clean"


def test_a_mapping_read_of_an_allowed_column_is_not_flagged(tmp_path):
    """The other direction: last4 is what the code is supposed to read."""
    hits = _run_checker_over(tmp_path, (
        "def display(row):\n"
        '    return row["last4"], row["brand"]\n'
    ))
    assert hits == [], f"unexpected hits: {hits}"


# --- SQL that is assembled rather than written out ----------------------------
#
# Reviewed on PR #15: a checker that only reads bare literals misses every
# common way a query gets built. Each of these is statically resolvable, so the
# checker can prove the read without running anything.

def test_adjacent_string_literals_are_scanned(tmp_path):
    hits = _run_checker_over(tmp_path, (
        "def read(conn):\n"
        '    return conn.execute(\n'
        '        "SELECT id, "\n'
        '        "pan "\n'
        '        "FROM payments"\n'
        "    )\n"
    ))
    assert hits, "adjacent literals were reported clean"


def test_explicit_concatenation_is_scanned(tmp_path):
    hits = _run_checker_over(tmp_path, (
        "def read(conn):\n"
        '    return conn.execute("SELECT " + "pan FROM payments")\n'
    ))
    assert hits, "concatenated SQL was reported clean"


def test_multiline_concatenation_is_scanned(tmp_path):
    hits = _run_checker_over(tmp_path, (
        "def read(conn):\n"
        '    sql = ("SELECT id, "\n'
        '           + "pan, "\n'
        '           + "last4 "\n'
        '           + "FROM payments")\n'
        "    return conn.execute(sql)\n"
    ))
    assert hits, "multiline concatenation was reported clean"


def test_an_fstring_query_is_scanned(tmp_path):
    """The literal parts are what name the columns; the hole is a value."""
    hits = _run_checker_over(tmp_path, (
        "def read(conn, loan_id):\n"
        '    return conn.execute(f"SELECT pan FROM payments WHERE loan_id = {loan_id}")\n'
    ))
    assert hits, "an f-string query was reported clean"


def test_format_and_percent_templates_are_scanned(tmp_path):
    """The template is the statement; the arguments are parameters."""
    formatted = _run_checker_over(tmp_path, (
        "def read(conn, table):\n"
        '    return conn.execute("SELECT pan FROM {}".format(table))\n'
    ))
    assert formatted, "a .format() template was reported clean"

    percent = _run_checker_over(tmp_path, (
        "def read(conn, loan_id):\n"
        '    return conn.execute("SELECT cvv FROM payments WHERE id = %s" % (loan_id,))\n'
    ))
    assert percent, "a %-formatted template was reported clean"


def test_sql_assigned_to_a_variable_then_executed_is_scanned(tmp_path):
    """The shape the reviewer named: built here, executed there."""
    hits = _run_checker_over(tmp_path, (
        'LEGACY = "SELECT pan, cvv FROM payments WHERE loan_id = %s"\n'
        "\n"
        "def read(conn, loan_id):\n"
        "    return conn.execute(LEGACY, (loan_id,))\n"
    ))
    assert hits, "SQL carried by a variable was reported clean"


def test_clean_assembled_sql_passes(tmp_path):
    """Every one of those forms, reading only what the code is allowed to."""
    hits = _run_checker_over(tmp_path, (
        'BASE = "SELECT id, last4, brand "\n'
        "\n"
        "def read(conn, loan_id):\n"
        '    sql = BASE + "FROM payments WHERE loan_id = %s"\n'
        "    return conn.execute(sql, (loan_id,))\n"
        "\n"
        "def other(conn, loan_id):\n"
        '    return conn.execute(f"SELECT last4 FROM payments WHERE id = {loan_id}")\n'
    ))
    assert hits == [], f"clean assembled SQL was flagged: {hits}"


def test_documentation_about_the_columns_is_still_ignored(tmp_path):
    """Prose is excluded by position, and that must survive the folding."""
    hits = _run_checker_over(tmp_path, (
        '"""This service used to SELECT pan, cvv FROM payments.\n'
        "\n"
        "The columns are dropped by 0031; see docs/DEBT.md D5b.\n"
        '"""\n'
        "\n"
        "def read(conn):\n"
        '    """Return display fields only -- never SELECT pan or cvv."""\n'
        '    return conn.execute("SELECT last4 FROM payments")\n'
    ))
    assert hits == [], f"documentation was reported as a live read: {hits}"


def test_a_statically_known_fstring_substitution_is_resolved(tmp_path):
    """`COL = "pan"` then `f"SELECT {COL} FROM payments"` is a live read.

    Leaving the substitution as a hole hid it: the standalone `"pan"` carries no
    SQL context of its own, so nothing else would catch it, and the checker
    returned clean. Only names already resolved statically are substituted --
    anything genuinely runtime stays a hole. Reviewed on PR #15.
    """
    hits = _run_checker_over(tmp_path, (
        'COL = "pan"\n'
        "\n"
        "def read(conn):\n"
        '    return conn.execute(f"SELECT {COL} FROM payments")\n'
    ))
    assert hits, "an f-string substituting a known column name was reported clean"


def test_a_runtime_substitution_in_the_projection_fails_closed(tmp_path):
    """Superseded expectation, corrected.

    This used to assert that `execute(f"SELECT {column} FROM payments")` was
    clean, on the grounds that the hole was not a known column. Review on PR #15
    called that out: the runtime query can select anything, so a hole in the
    PROJECTION is an unresolved statement, not a clean one. A hole in a value
    position -- `WHERE id = {loan_id}` -- is still fine and is covered
    separately.
    """
    hits = _run_checker_over(tmp_path, (
        "def read(conn, column):\n"
        '    return conn.execute(f"SELECT {column} FROM payments")\n'
    ))
    assert hits, "an unresolved projection reaching execute() was reported clean"
    assert any("unresolved dynamic SQL" in h[3] for h in hits), hits


def test_a_runtime_value_in_a_where_clause_is_still_clean(tmp_path):
    """Ordinary parameterised SQL must not be refused.

    Failing closed on every hole would refuse most legitimate queries here and
    leave the checker permanently red.
    """
    hits = _run_checker_over(tmp_path, (
        "def read(conn, loan_id):\n"
        '    return conn.execute(f"SELECT last4 FROM payments WHERE id = {loan_id}")\n'
    ))
    assert hits == [], f"parameterised SQL was flagged: {hits}"


def test_a_statically_known_substitution_of_an_allowed_column_passes(tmp_path):
    """...and resolving it must not invent a hit either."""
    hits = _run_checker_over(tmp_path, (
        'COL = "last4"\n'
        "\n"
        "def read(conn):\n"
        '    return conn.execute(f"SELECT {COL} FROM payments")\n'
    ))
    assert hits == [], f"clean SQL was flagged: {hits}"


def test_a_local_binding_does_not_vouch_for_another_function(tmp_path):
    """A name means what it means WHERE IT IS WRITTEN.

    Bindings were collected in one pass over the whole file, so a clean
    `COL = "last4"` in one function could be the value used when folding a
    different function's `f"SELECT {COL} FROM payments"` -- masking a live read,
    or inventing one. Reviewed on PR #15.
    """
    hits = _run_checker_over(tmp_path, (
        "def clean(conn):\n"
        '    COL = "last4"\n'
        '    return conn.execute(f"SELECT {COL} FROM payments")\n'
        "\n"
        "def legacy(conn):\n"
        '    COL = "pan"\n'
        '    return conn.execute(f"SELECT {COL} FROM payments")\n'
    ))
    assert len(hits) == 1, f"expected exactly the legacy read, got: {hits}"
    # line 7 is `legacy`'s f-string; `clean`'s identical statement is line 3.
    assert hits[0][1] == 7, f"the hit is on the wrong line: {hits}"


def test_a_module_level_binding_is_visible_inside_a_function(tmp_path):
    """Inheritance still works: an outer name resolves in an inner scope."""
    hits = _run_checker_over(tmp_path, (
        'COL = "pan"\n'
        "\n"
        "def read(conn):\n"
        '    return conn.execute(f"SELECT {COL} FROM payments")\n'
    ))
    assert hits, "a module-level binding was not visible inside the function"


def test_an_inner_rebinding_shadows_the_module_value(tmp_path):
    """Innermost wins, as it would at runtime."""
    hits = _run_checker_over(tmp_path, (
        'COL = "pan"\n'
        "\n"
        "def read(conn):\n"
        '    COL = "last4"\n'
        '    return conn.execute(f"SELECT {COL} FROM payments")\n'
    ))
    assert hits == [], f"a shadowed module value produced a false hit: {hits}"


# --- SQL resolved at each execution's own program point -----------------------
#
# Two review findings on PR #15. The checker folded `.format()` down to its
# template, discarding arguments, and it resolved names against a scope's FINAL
# binding map -- so a rebinding BELOW an execute() decided what that execute()
# meant. Both let a live read of pan exit clean, and a clean exit is what
# authorises dropping the column.

def test_format_argument_naming_a_legacy_column_is_detected(tmp_path):
    hits = _run_checker_over(tmp_path, (
        "def read(conn):\n"
        '    return conn.execute("SELECT {} FROM payments".format("pan"))\n'
    ))
    assert hits, "a .format() argument naming pan was reported clean"


def test_a_safe_static_format_query_passes(tmp_path):
    hits = _run_checker_over(tmp_path, (
        "def read(conn):\n"
        '    return conn.execute("SELECT {} FROM payments".format("last4"))\n'
    ))
    assert hits == [], f"a clean .format() query was flagged: {hits}"


def test_an_unresolved_format_argument_fails_closed(tmp_path):
    """Cannot be resolved means cannot be cleared."""
    hits = _run_checker_over(tmp_path, (
        "def read(conn, col):\n"
        '    return conn.execute("SELECT {} FROM payments".format(col))\n'
    ))
    assert hits, "unresolvable SQL reaching execute() was reported clean"
    assert any("unresolved dynamic SQL" in h[3] for h in hits), hits


def test_a_later_rebinding_cannot_clear_an_earlier_execution(tmp_path):
    """The reviewed defect: the assignment below decided the call above."""
    hits = _run_checker_over(tmp_path, (
        "def read(conn):\n"
        '    COL = "pan"\n'
        '    conn.execute("SELECT " + COL + " FROM payments")\n'
        '    COL = "last4"\n'
        "    return COL\n"
    ))
    assert hits, "a live read was cleared by a rebinding that runs after it"
    assert all(h[1] == 3 for h in hits), f"reported at the wrong line: {hits}"


def test_a_later_rebinding_to_a_legacy_column_is_not_a_hit_on_its_own(tmp_path):
    """...and the mirror: rebinding alone, with no execution, reads nothing."""
    hits = _run_checker_over(tmp_path, (
        "def read(conn):\n"
        '    COL = "last4"\n'
        '    conn.execute("SELECT " + COL + " FROM payments")\n'
        '    COL = "pan"\n'
        "    return COL\n"
    ))
    assert hits == [], f"a name never executed was reported as a read: {hits}"


def test_each_execution_is_evaluated_at_its_own_program_point(tmp_path):
    """Two calls, two bindings: the second is the live read, the first is not."""
    hits = _run_checker_over(tmp_path, (
        "def read(conn):\n"
        '    COL = "last4"\n'
        '    conn.execute("SELECT " + COL + " FROM payments")\n'
        '    COL = "pan"\n'
        '    conn.execute("SELECT " + COL + " FROM payments")\n'
    ))
    assert hits, "the second execution's read of pan was missed"
    assert all(h[1] == 5 for h in hits), f"the clean first call was flagged too: {hits}"


def test_an_unresolved_fstring_field_fails_closed(tmp_path):
    """A hole in SQL that reaches the database is unresolved, not clean.

    `f"SELECT {column} FROM payments"` folded to `SELECT ? FROM payments` --
    a string with no column name, which read as clean while the runtime query
    could select anything. Consistent with `.format()`. Reviewed on PR #15.
    """
    hits = _run_checker_over(tmp_path, (
        "def read(conn, column):\n"
        '    return conn.execute(f"SELECT {column} FROM payments")\n'
    ))
    assert hits, "an unresolved f-string field reaching execute() was reported clean"
    assert any("unresolved dynamic SQL" in h[3] for h in hits), hits


def test_a_conditional_rebinding_is_not_assumed_to_have_run(tmp_path):
    """One branch must not clear a query built from the other.

    `COL = "pan"; if migrated: COL = "last4"; execute(...)` reads pan whenever
    the branch does not run, and walking the body as though it always did
    cleared it. Rather than model both branches -- the control-flow analysis
    this tool does not do -- the name becomes uncertain and the query fails
    closed. Reviewed on PR #15.
    """
    hits = _run_checker_over(tmp_path, (
        "def read(conn, migrated):\n"
        '    COL = "pan"\n'
        "    if migrated:\n"
        '        COL = "last4"\n'
        '    return conn.execute("SELECT " + COL + " FROM payments")\n'
    ))
    assert hits, "a conditionally rebound name cleared a live read"


def test_an_unconditional_rebinding_still_resolves(tmp_path):
    """The uncertainty is about branches, not about assignment in general."""
    hits = _run_checker_over(tmp_path, (
        "def read(conn):\n"
        '    COL = "pan"\n'
        '    COL = "last4"\n'
        '    return conn.execute("SELECT " + COL + " FROM payments")\n'
    ))
    assert hits == [], f"a straight-line rebinding was treated as uncertain: {hits}"


def test_an_orm_construct_built_in_a_branch_is_not_dynamic_sql(tmp_path):
    """A name that was never a SQL string stays opaque.

    `stmt = select(...)` refined inside an `if` is a query construct, not string
    assembly; failing closed on it would refuse every ORM statement in the
    repository and leave the checker permanently red.
    """
    hits = _run_checker_over(tmp_path, (
        "def read(session, status):\n"
        "    stmt = select(Loan)\n"
        "    if status:\n"
        "        stmt = stmt.where(Loan.status == status)\n"
        "    return session.execute(stmt).all()\n"
    ))
    assert hits == [], f"an ORM construct was reported as dynamic SQL: {hits}"


def test_no_source_file_contains_a_control_character_escape():
    """A literal U+0008 where `\b` was intended silently disables a pattern.

    This has happened twice in this tool -- once in the unit pattern, once in
    the FROM boundary -- both times because an escape was written through a
    shell heredoc that interpreted it. The symptom is invisible: the regex still
    compiles, matches nothing, and the checker reports clean. Cheap to assert
    mechanically, so it is asserted here rather than noticed later.
    """
    for path in [
        REPO_ROOT / "db" / "tools" / "check_no_pan_readers.py",
        REPO_ROOT / "db" / "tests" / "test_0031_contract_gate.py",
    ]:
        raw = path.read_bytes()
        for code in (0x08, 0x00, 0x0C, 0x1B):
            assert bytes([code]) not in raw, (
                f"{path.name} contains a raw control character 0x{code:02x} -- "
                f"almost certainly an escape sequence that was interpreted "
                f"instead of written literally"
            )


def test_unresolved_sql_reaching_db_query_fails_closed(tmp_path):
    """`db.query` is the raw-SQL entry point in this repository.

    Fail-closed covered `execute` only, so the path most queries here actually
    take was exempt. Reviewed on PR #15.
    """
    hits = _run_checker_over(tmp_path, (
        "def read(db, column):\n"
        '    return db.query(f"SELECT {column} FROM payments")\n'
    ))
    assert hits, "unresolved SQL via db.query was reported clean"
    assert any("unresolved dynamic SQL" in h[3] for h in hits), hits


def test_unresolved_sql_on_another_table_does_not_block_the_drop(tmp_path):
    """Fail-closed is about the table whose columns are being dropped.

    An unresolved projection over `applications` cannot read payments.pan, and
    refusing it would block this migration over a query with nothing to do with
    it. This repository builds several projections from cross-module constants,
    which are out of this tool's reach by design.
    """
    hits = _run_checker_over(tmp_path, (
        "def read(db, fields):\n"
        '    return db.query(f"SELECT {fields} FROM applications WHERE id = %s", (1,))\n'
    ))
    assert hits == [], f"an unrelated table's query blocked the drop: {hits}"


def test_a_projection_joined_from_a_literal_tuple_is_resolved(tmp_path):
    """How every projection in this repository is actually built."""
    legacy = _run_checker_over(tmp_path, (
        'COLUMNS = ("id", "pan", "last4")\n'
        'FIELDS = ", ".join(COLUMNS)\n'
        "\n"
        "def read(db):\n"
        '    return db.query(f"SELECT {FIELDS} FROM payments")\n'
    ))
    assert legacy, "a projection joined from a tuple containing pan was reported clean"

    clean = _run_checker_over(tmp_path, (
        'COLUMNS = ("id", "last4", "brand")\n'
        'FIELDS = ", ".join(COLUMNS)\n'
        "\n"
        "def read(db):\n"
        '    return db.query(f"SELECT {FIELDS} FROM payments")\n'
    ))
    assert clean == [], f"a clean joined projection was flagged: {clean}"


def test_an_expression_inside_a_branch_is_judged_with_that_branch_state(tmp_path):
    """The initial walk must not evaluate nested bodies with outer bindings.

    Walking a statement's own expressions used to descend into its `if` body,
    so those expressions were judged twice -- once against the bindings from
    BEFORE the branch. Reviewed on PR #15.
    """
    hits = _run_checker_over(tmp_path, (
        "def read(conn, flag):\n"
        '    COL = "last4"\n'
        "    if flag:\n"
        '        COL = "pan"\n'
        '        conn.execute("SELECT " + COL + " FROM payments")\n'
    ))
    assert hits, "a read inside a branch was judged with pre-branch bindings"
    assert all(h[1] == 5 for h in hits), f"reported at the wrong line: {hits}"


def test_a_nested_function_does_not_rely_on_a_later_rebinding(tmp_path):
    """A closure reads its outer name when it RUNS, not when it is defined."""
    hits = _run_checker_over(tmp_path, (
        "def outer(conn):\n"
        '    COL = "last4"\n'
        "\n"
        "    def inner():\n"
        '        return conn.execute("SELECT " + COL + " FROM payments")\n'
        "\n"
        '    COL = "pan"\n'
        "    return inner\n"
    ))
    assert hits, "a closure was cleared by the binding at its definition point"


# --- annotated assignments bind like unannotated ones -------------------------
#
# Reviewed on PR #15. `sql: str = ...` is the same statement as `sql = ...` with a
# type annotation on it, and the checker handled only `ast.Assign` at three of its
# four binding sites. So an annotated assignment bound nothing: the name stayed
# unknown, no unresolved state was recorded for it, and `db.query(sql)` carried no
# table literal of its own -- the run reported clean over a live dynamic read.
# Annotating a line is not a semantic change and must not be a way through.

_DYNAMIC_ANNOTATED = '''
import os

_COLUMN = os.environ["COL"]


def read():
    sql: str = "SELECT " + _COLUMN + " FROM payments"
    return db.query(sql)
'''

_DYNAMIC_PLAIN = _DYNAMIC_ANNOTATED.replace("sql: str =", "sql =")


def test_an_annotated_dynamic_sql_assignment_fails_closed(tmp_path):
    """The reported case. Refused, not reported clean."""
    hits = _run_checker_over(tmp_path, _DYNAMIC_ANNOTATED)
    assert hits, (
        "an annotated assignment of dynamically composed payments SQL was "
        "reported clean; it could authorize the destructive migration"
    )


def test_annotation_does_not_change_the_verdict(tmp_path):
    """The two forms are the same statement, so they must agree.

    Asserted as a PAIR rather than only on the annotated form: a checker that
    refused everything would pass the test above while being useless, and this
    pins the equivalence rather than one arbitrary outcome.
    """
    annotated = _run_checker_over(tmp_path, _DYNAMIC_ANNOTATED)
    plain = _run_checker_over(tmp_path, _DYNAMIC_PLAIN)
    assert bool(annotated) == bool(plain), (
        f"annotation changed the verdict: annotated={annotated!r} plain={plain!r}"
    )


def test_an_annotated_static_statement_is_still_clean(tmp_path):
    """The other direction: annotations must not manufacture findings either."""
    hits = _run_checker_over(tmp_path, '''
def read():
    sql: str = "SELECT last4, brand FROM payments WHERE id = %s"
    return db.query(sql, (1,))
''')
    assert not hits, f"a static annotated statement was reported as a reader: {hits!r}"


def test_an_annotation_without_a_value_binds_nothing(tmp_path):
    """`sql: str` declares a name and assigns nothing.

    Treating it as a binding would either crash on the missing value or wipe a
    real one, so it is skipped -- and the real assignment above it still governs.
    """
    hits = _run_checker_over(tmp_path, '''
def read():
    sql: str
    sql = "SELECT pan FROM payments"
    return db.query(sql)
''')
    assert hits, "the declaration swallowed the assignment that followed it"
