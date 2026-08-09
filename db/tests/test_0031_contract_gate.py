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
    svc.mkdir(parents=True)
    (svc / "reader.py").write_text(source, encoding="utf-8")
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
