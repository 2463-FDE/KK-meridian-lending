"""The runbook's follow-one-payment procedure must still be runnable.

`db/migrations/0043` gave an operator a way to answer "you charged me and my
balance did not move" without joining two services' logs by eye. That capability
is worth nothing to the person it was built for unless the procedure is written
down, and a written procedure is worth nothing once it stops matching the
schema. This repository's whole history of stale claims is documents drifting
from code, and an operator following a dead runbook mid-incident is the most
expensive version of it.

So the procedure's load-bearing facts are asserted rather than trusted: the
columns it queries exist on both schema paths, the indexes it promises exist,
and the two "no id" cases it describes are the ones the migration actually
creates.

Deliberately NOT asserted: the prose. A test that pinned wording would fail on
every edit and be deleted within a month. What is pinned is every claim an
operator would type into a shell.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
RUNBOOK = REPO / "docs" / "runbook.md"
MIGRATION = REPO / "db" / "migrations" / "0043_correlation_id.sql"
INIT = REPO / "db" / "init" / "001_schema.sql"

SECTION = "## Following one payment across services"


def _section() -> str:
    text = RUNBOOK.read_text(encoding="utf-8")
    assert SECTION in text, (
        "the runbook no longer has a follow-one-payment procedure -- the "
        "correlation id shipped so an operator could use it, and this is where "
        "they are told how"
    )
    body = text.split(SECTION, 1)[1]
    return body.split("\n## ", 1)[0]


def test_the_section_exists_and_is_not_a_stub():
    """Guard the guard: an empty section would satisfy every check below."""
    body = _section()

    assert len(body) > 800, f"the procedure is {len(body)} characters -- too thin to follow"
    assert "```" in body, "the procedure gives no commands to run"


@pytest.mark.parametrize("table", ["payments", "ledger_entries"])
def test_every_table_the_procedure_queries_has_the_column(table):
    """The queries are `WHERE correlation_id = ...` on these two tables.

    Checked against BOTH schema paths, because a fresh install and a migrated
    database are different files and an operator's database may be either.
    """
    body = _section()
    assert f"FROM {table}" in body, f"the procedure no longer queries {table}"

    assert f"ALTER TABLE {table}" in MIGRATION.read_text(encoding="utf-8").replace(
        "ALTER TABLE payments        ", "ALTER TABLE payments ").replace(
        "ALTER TABLE ledger_entries  ", "ALTER TABLE ledger_entries "), (
        f"{MIGRATION.name} does not add correlation_id to {table}"
    )

    init = INIT.read_text(encoding="utf-8")
    block = init.split(f"CREATE TABLE IF NOT EXISTS {table}", 1)
    assert len(block) == 2, f"{table} is not created by the fresh-install schema"
    assert "correlation_id" in block[1].split(");", 1)[0], (
        f"a fresh install has no correlation_id on {table}, so the runbook's "
        f"query fails on exactly the database a new deployment has"
    )


def test_the_procedure_does_not_promise_an_index_that_does_not_exist():
    """It tells the operator neither query scans the table. That is a claim
    about two partial indexes, and an operator running this against a large
    payments table would find out the hard way."""
    body = _section()
    if "index" not in body.lower():
        pytest.skip("the procedure no longer claims anything about indexes")

    migration = MIGRATION.read_text(encoding="utf-8")
    for index in ("idx_payments_correlation_id", "idx_ledger_entries_correlation_id"):
        assert index in migration, f"{index} is promised but not created"
        assert "WHERE correlation_id IS NOT NULL" in migration


def test_the_columns_the_triage_table_reads_exist():
    """The reading-the-answer table sends an operator at specific columns. A
    renamed column turns triage into a syntax error at the worst moment."""
    body = _section()
    init = INIT.read_text(encoding="utf-8")
    payments = init.split("CREATE TABLE IF NOT EXISTS payments", 1)[1].split(");", 1)[0]

    for column in ("apply_attempts", "apply_last_error", "auth_status",
                   "captured_at", "applied_at", "processor_ref"):
        assert column in body, f"the triage table stopped mentioning {column}"
        assert column in payments, f"payments has no {column} column"


def test_the_two_no_id_cases_are_the_ones_the_schema_creates():
    """The procedure tells an operator when to expect NULL. Both cases have to
    be real, or they read as excuses for a broken trace."""
    body = _section().lower()
    migration = MIGRATION.read_text(encoding="utf-8").lower()

    assert "null" in body, "the procedure never mentions the absent-id cases"
    # Case 1: rows written before the column existed.
    assert "before 0043" in body or "before this column" in body
    assert "nullable" in migration or "null means" in migration
    # Case 2: ledger entries with no payment behind them.
    assert "no payment behind it" in body or "no payment behind them" in body
    assert "no payment" in migration


def test_it_does_not_claim_the_id_decides_anything():
    """The correlator is inert, and the runbook says so.

    That sentence is what makes it safe to quote in a ticket, and it is the
    distinction that stops someone treating it as an idempotency key later.
    """
    body = _section().lower()

    assert "idempotency key" in body, (
        "the procedure does not distinguish the correlator from the idempotency "
        "key, which is the confusion most likely to cause harm here"
    )
    assert re.search(r"correlates and nothing else|no balance.*depends on it",
                     body), "the procedure does not say the id is inert"
