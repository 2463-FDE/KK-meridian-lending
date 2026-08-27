"""The offline fairness evaluation stays offline, aggregate, and verdict-free.

Three separate promises, and each fails in a different direction:

  * **Offline** — an evaluator that acquires a database handle or an HTTP client
    has become a runtime path, and the labels it reads are the ones the client
    said may never reach runtime.
  * **Aggregate** — a per-row output carries a synthetic protected-class label
    attached to an identifier, which is what "audit-only" exists to prevent.
  * **Verdict-free** — the client's EVAL-16 rejects the claim "the model is fair
    based on the 32-row fixture" by name. A tool that emits a four-fifths pass
    would be making exactly that claim on the repository's behalf, and it would
    be the most quotable line in its own output.

The third is the one that would survive review most easily, because a fairness
number looks like diligence. It is the one the client wrote a case about.
"""
import ast
import io
import json
import pathlib
import sys
from contextlib import redirect_stdout

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
TOOLS = REPO / "db" / "tools"
sys.path.insert(0, str(TOOLS))

import offline_fairness_eval as evaluator  # noqa: E402
from client_governance_package import (  # noqa: E402
    PROTECTED_CLASS_COLUMNS,
    TRAINING_BANNER,
    load_fairness_fixture,
)


@pytest.fixture(scope="module")
def result():
    return evaluator.aggregate(load_fairness_fixture())


@pytest.fixture(scope="module")
def rendered(result):
    return evaluator.render(result)


def test_it_reads_the_thirty_two_synthetic_rows(result):
    assert result["total_rows"] == 32, (
        "the fixture no longer holds the 32 rows the client shipped; the "
        "checksum test should have caught that first")


def test_it_groups_by_every_protected_class_column(result):
    assert set(result["groups"]) == set(PROTECTED_CLASS_COLUMNS)
    for column, groups in result["groups"].items():
        assert sum(g["n"] for g in groups.values()) == 32, (
            f"{column} does not account for every row")


def test_it_computes_no_verdict(result):
    """The client's EVAL-16, enforced against our own tool."""
    assert result["verdict"] is None
    assert "EVAL-16" in result["verdict_withheld_because"]

    banned = ("four-fifths", "80%", "disparate impact", "passes", "compliant",
              "is fair", "no bias", "unbiased")
    blob = json.dumps(result).lower()
    for phrase in banned:
        assert phrase not in blob, (
            f"the aggregate contains {phrase!r}, which reads as a fairness "
            f"verdict the package does not authorise")


def test_the_output_carries_no_individual_row(result, rendered):
    """Aggregate only. No fixture row id may appear in either form of output."""
    ids = [r["fixture_row_id"] for r in load_fairness_fixture()]
    blob = json.dumps(result)
    for row_id in ids:
        assert row_id not in blob, f"{row_id} reached the JSON aggregate"
        assert row_id not in rendered, f"{row_id} reached the rendered report"

    # Scores are per-record too, and are not aggregated here.
    assert "synthetic_score_label" not in blob


def test_both_output_forms_say_what_they_are(result, rendered):
    assert result["banner"] == TRAINING_BANNER
    assert TRAINING_BANNER in rendered
    assert rendered.strip().endswith(f"=== END — {TRAINING_BANNER} ===")
    for phrase in ("Not vendor-issued", "not", "production evidence"):
        assert phrase.lower() in rendered.lower()


def test_the_cli_runs_and_stays_labelled():
    buf = io.StringIO()
    with redirect_stdout(buf):
        rc = evaluator.main([])
    out = buf.getvalue()

    assert rc == 0
    assert TRAINING_BANNER in out
    assert "VERDICT: none" in out


def test_the_cli_json_form_is_parseable_and_verdict_free():
    buf = io.StringIO()
    with redirect_stdout(buf):
        evaluator.main(["--json"])
    payload = json.loads(buf.getvalue())

    assert payload["verdict"] is None
    assert payload["checksums_verified"] == 34


def test_the_evaluator_opens_no_database_and_no_socket():
    """Structural: read the module's imports rather than trusting the docstring.

    A network or database import here would not fail a behavioural test — the
    tool simply would not call it today — so the check is on what the module can
    reach at all.
    """
    forbidden = {"psycopg", "psycopg2", "sqlalchemy", "httpx", "requests",
                 "socket", "urllib", "boto3", "langchain", "langchain_aws"}
    offenders = []

    for path in (TOOLS / "offline_fairness_eval.py",
                 TOOLS / "client_governance_package.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module.split(".")[0]]
            offenders += [f"{path.name}: {n}" for n in names if n in forbidden]

    assert offenders == [], (
        "the offline evaluator can reach a database, a network client or a "
        f"model: {offenders}. It reads the client package and nothing else.")


def test_the_evaluator_names_no_runtime_table():
    """It must not query applicants, applications, decisions or decision_events."""
    for path in (TOOLS / "offline_fairness_eval.py",
                 TOOLS / "client_governance_package.py"):
        body = path.read_text(encoding="utf-8")
        for table in ("applicants", "applications", "decisions", "decision_events"):
            # The docstrings name these tables to say they are never queried,
            # so look for SQL rather than for the word.
            assert f"FROM {table}" not in body and f"from {table} " not in body, (
                f"{path.name} appears to query {table}")


def test_the_fixture_labels_reach_no_runtime_service():
    """The values, not just the column names.

    A column name could be renamed; the label values are what the client cares
    about. `SYN-Black` appearing in a service file would mean a synthetic label
    had been copied into runtime code.
    """
    values = set()
    for row in load_fairness_fixture():
        for column in PROTECTED_CLASS_COLUMNS:
            if row.get(column):
                values.add(row[column])

    offenders = []
    for path in sorted((REPO / "services").rglob("*.py")):
        if "tests" in path.parts or "__pycache__" in path.parts:
            continue
        body = path.read_text(encoding="utf-8", errors="replace")
        for value in values:
            if value in body:
                offenders.append(f"{path.relative_to(REPO)}: {value}")

    assert offenders == [], (
        "synthetic protected-class label values appear in runtime service "
        "code:\n  " + "\n  ".join(offenders))
