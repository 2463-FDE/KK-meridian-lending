"""A reconciliation run that compared nothing must never look successful.

`within_threshold` was computed purely as `break_value <= BREAK_THRESHOLD`. An
empty settlement file, a file with no usable `settlement_date`, and a file whose
loans match nothing on the ledger all produce zero breaks -- because nothing was
checked -- and therefore zero break value. So the run recorded `outcome='ok'`,
created a `last_successful_run`, and published a fresh success timestamp.

That is the worst available failure for this control. D7 exists because a
control that silently is not running looks exactly like one that is running and
finding nothing; recording success for a comparison that never happened is the
same defect one layer up, and it defeats the monitoring built on top of it --
the stale-success alarm stays quiet precisely when the feed is broken.

These tests run against real PostgreSQL because the assertion is about what was
RECORDED: the run row, the last-success query and the exit code. A mock proves
none of that.
"""
import os
import pathlib

import psycopg2
import psycopg2.extras
import pytest

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against"
)

REPO = pathlib.Path(__file__).resolve().parents[3]
SCHEMA = "reconciliation_vacuous_test"

HEADER = "loan_id,amount,type,settlement_date\n"

# Every way a run can compare nothing, with the code each must record.
VACUOUS_FILES = [
    pytest.param("", "EmptySettlementFile", id="completely-empty"),
    pytest.param(HEADER, "EmptySettlementFile", id="headers-only"),
    pytest.param(
        HEADER + "1,100.00,capture,\n2,50.00,capture,\n",
        "IncompleteSettlementWindow",
        id="no-settlement-dates",
    ),
]


@pytest.fixture
def db(monkeypatch):
    from app import reconciliation

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute((REPO / "db" / "init" / "001_schema.sql").read_text(encoding="utf-8"))

    scoped = f"{DATABASE_URL}?options=-csearch_path%3D{SCHEMA}"
    for module_attr in ("DATABASE_URL",):
        monkeypatch.setattr(reconciliation.db, module_attr, scoped, raising=False)
    monkeypatch.setattr(reconciliation.db, "_conn", None, raising=False)
    yield conn
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.close()
    monkeypatch.setattr(reconciliation.db, "_conn", None, raising=False)


def _runs(conn):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT outcome, error_code, loans_compared FROM reconciliation_runs "
                    "ORDER BY id")
        return cur.fetchall()


def _write(tmp_path, content):
    path = tmp_path / "settlement.csv"
    path.write_text(content, encoding="utf-8")
    return str(path)


@pytest.mark.parametrize("content,expected_code", VACUOUS_FILES)
def test_a_vacuous_file_records_an_error_not_an_ok(db, tmp_path, monkeypatch, content, expected_code):
    from app import reconcile_job, reconciliation

    monkeypatch.setattr(reconciliation, "SETTLEMENT_FILE",
                        _write(tmp_path, content), raising=False)

    exit_code = reconcile_job.main([])

    rows = _runs(db)
    assert rows, "no run was recorded at all"
    final = rows[-1]
    assert final["outcome"] == "error", (
        f"a run that compared nothing recorded outcome={final['outcome']!r}. "
        "That creates a last_successful_run and a fresh success timestamp for a "
        "comparison that never happened."
    )
    assert final["error_code"] == expected_code
    assert exit_code == reconcile_job.EXIT_ERROR


@pytest.mark.parametrize("content,_code", VACUOUS_FILES)
def test_a_vacuous_run_never_becomes_the_last_success(db, tmp_path, monkeypatch, content, _code):
    """The property the monitoring actually reads.

    `last_successful_run` and the Prometheus success timestamp are both derived
    from rows with outcome='ok'. If a vacuous run wrote one, the stale-success
    alarm would stay quiet exactly while the feed was broken.
    """
    from app import reconcile_job, reconciliation

    monkeypatch.setattr(reconciliation, "SETTLEMENT_FILE",
                        _write(tmp_path, content), raising=False)
    reconcile_job.main([])

    with db.cursor() as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute("SELECT count(*) FROM reconciliation_runs WHERE outcome = 'ok'")
        assert cur.fetchone()[0] == 0, (
            "a vacuous run produced an 'ok' row, so the last-success timestamp "
            "advanced without anything being compared"
        )


def test_rows_present_but_nothing_comparable_is_also_vacuous(db, tmp_path, monkeypatch):
    """The subtlest case: a well-formed file about loans we do not service.

    Rows are read and a window exists, so the first two checks pass. Nothing
    matches the ledger, `loans_compared` is 0, and zero breaks follow from zero
    comparisons -- which read as a clean run.
    """
    from app import reconcile_job, reconciliation

    # No loans seeded, so the ledger side is empty; these ids match nothing.
    monkeypatch.setattr(
        reconciliation, "SETTLEMENT_FILE",
        _write(tmp_path, HEADER), raising=False,
    )
    exit_code = reconcile_job.main([])
    assert exit_code == reconcile_job.EXIT_ERROR
    assert _runs(db)[-1]["outcome"] == "error"


def test_the_vacuity_check_is_ordered_so_the_code_names_the_real_cause(db):
    """An empty file also has no window and compares nothing. The operator
    should be told the file was empty, not that the window was unusable -- the
    three codes route to different people."""
    from app import reconciliation

    empty = {"source": {"rows": 0}, "window_start": None, "window_end": None,
             "loans_compared": 0}
    assert reconciliation.vacuity_error(empty)[0] == "EmptySettlementFile"

    no_window = {"source": {"rows": 5}, "window_start": None, "window_end": None,
                 "loans_compared": 0}
    assert reconciliation.vacuity_error(no_window)[0] == "IncompleteSettlementWindow"

    nothing = {"source": {"rows": 5}, "window_start": "2026-08-01",
               "window_end": "2026-08-01", "loans_compared": 0}
    assert reconciliation.vacuity_error(nothing)[0] == "NothingCompared"


def test_a_real_comparison_is_not_treated_as_vacuous(db):
    """Guard the guard.

    A check that called every run vacuous would pass every test above and break
    the control completely. This is the shape of a run that genuinely compared
    something.
    """
    from app import reconciliation

    real = {"source": {"rows": 12}, "window_start": "2026-08-01",
            "window_end": "2026-08-02", "loans_compared": 7}
    assert reconciliation.vacuity_error(real) is None
