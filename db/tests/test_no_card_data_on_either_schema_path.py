"""Neither a fresh database nor a fully migrated one has a PAN or CVV column.

Slide 2 of `docs/presentations/2026-08-12-three-slides.md` claims the removal was
verified on both. Two earlier attempts at that evidence were not good enough, and
the second one is the instructive failure:

  - the deck first cited the migration, the init schema and a legacy-path test.
    None of them compared the two paths, and the comparison *was* the claim;
  - the test written to fix that built the migrated schema from a hand-written
    `payments` table and wrapped the migration chain in `except psycopg2.Error:
    conn.rollback()`. Unrelated-but-required objects, constraints and ordering
    interactions could therefore fail, roll back, and never fail the test. It
    proved a property of a schema shape production never has.

This version uses the repository's real builders from `migration_paths.py` --
the same ones `test_migration_paths_converge.py` uses -- so "the migrated
database" means the same thing in both files. **Nothing is suppressed:** a
migration that fails, fails this test.

The real history is what makes this a removal rather than an absence:

    legacy schema          payments.pan exists, holding real card numbers
    0002_add_cvv           adds payments.cvv
    0029_backfill_last4    makes the drop survivable for readers
    0031_drop_pan_cvv      removes both

so the migrated path creates card columns partway through and must end without
them. Both halves of that are asserted below, because a proof that the columns
are absent at the end is worth nothing if they were never present.
"""
import os

import psycopg2
import psycopg2.extras
import pytest

from migration_paths import (
    _all_migrations,
    _apply_all_migrations,
    _build_fresh_init,
    _build_legacy_schema,
    _has_executable_sql,
    _run_sql,
)

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against"
)

FRESH = "cardpath_fresh"
MIGRATED = "cardpath_migrated"

# Any column that could hold a full card number or a security code. Substring
# matching, because `card_number`, `pan_encrypted` and `cvv2` are the same defect
# wearing a different name -- an exact-match test would pass on a rename.
FORBIDDEN_SUBSTRINGS = ("pan", "cvv", "cvc", "card_number", "cardnumber", "security_code")

# Columns that contain a forbidden substring and are legitimate. `company_name`
# contains "pan"; so does `expansion`. Listed explicitly so the check can stay a
# substring match without becoming unusable.
ALLOWED_EXACT = {"company_name", "plan", "plan_id", "expansion", "span", "japan"}


@pytest.fixture
def conn():
    connection = psycopg2.connect(DATABASE_URL)
    connection.autocommit = False
    with connection.cursor() as cur:
        for schema in (FRESH, MIGRATED):
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
            cur.execute(f"CREATE SCHEMA {schema}")
    connection.commit()
    yield connection
    with connection.cursor() as cur:
        for schema in (FRESH, MIGRATED):
            cur.execute(f"DROP SCHEMA IF EXISTS {schema} CASCADE")
    connection.commit()
    connection.close()


@pytest.fixture
def both_paths(conn):
    """A fresh volume, and a real pre-migration database brought fully up to date.

    Both calls raise on any failure. If the migration chain cannot run against
    the legacy schema, that is a broken upgrade path and this test says so,
    rather than quietly proving something about a partial database.
    """
    _build_fresh_init(conn, FRESH)
    _build_legacy_schema(conn, MIGRATED)
    _apply_all_migrations(conn, MIGRATED)
    return conn


def _card_columns(conn, schema):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = %s",
            (schema,),
        )
        rows = cur.fetchall()

    found = []
    for row in rows:
        name = row["column_name"].lower()
        if name in ALLOWED_EXACT:
            continue
        if any(bad in name for bad in FORBIDDEN_SUBSTRINGS):
            found.append(f"{row['table_name']}.{row['column_name']}")
    return rows, found


def test_a_fresh_database_has_no_card_columns(both_paths):
    rows, found = _card_columns(both_paths, FRESH)
    assert rows, "the fresh schema has no columns at all -- it was never built"
    assert not found, f"a fresh database creates card columns: {found}"


def test_a_fully_migrated_database_has_no_card_columns(both_paths):
    """The real upgrade path, end to end, with no errors suppressed."""
    rows, found = _card_columns(both_paths, MIGRATED)
    assert rows, "the migrated schema has no columns at all -- it was never built"
    assert not found, f"card columns survive the real migration chain: {found}"


def test_the_legacy_starting_point_really_held_a_card_number(conn):
    """Guard the guard, part one.

    Without this, the migrated assertion would pass on a legacy fixture that
    never had a PAN -- proving the removal works by having nothing to remove.
    This repository has shipped a vacuous check before.
    """
    _build_legacy_schema(conn, MIGRATED)
    _, found = _card_columns(conn, MIGRATED)
    assert "payments.pan" in found, (
        f"the legacy schema no longer starts with payments.pan: {found}. The "
        "migrated-path proof would then be asserting an absence, not a removal."
    )


def test_the_chain_really_creates_a_cvv_before_dropping_it(conn):
    """Guard the guard, part two.

    `cvv` is not in the legacy schema -- `0002_add_cvv_to_payments.sql` adds it
    and `0031` drops it. The CVV half of the claim is only meaningful if the
    column genuinely exists partway through, so the chain is stopped right
    after 0002 and the column checked for.
    """
    _build_legacy_schema(conn, MIGRATED)
    for path in _all_migrations():
        sql = path.read_text(encoding="utf-8")
        if _has_executable_sql(sql):
            _run_sql(conn, MIGRATED, sql)
        if path.name.startswith("0002"):
            break

    _, found = _card_columns(conn, MIGRATED)
    assert "payments.cvv" in found, (
        f"0002 no longer introduces payments.cvv: {found}. The migrated path "
        "would then never hold a security code, and 0031 dropping it would "
        "prove nothing."
    )


def test_the_two_paths_agree_on_the_payments_table(both_paths):
    """The sentence on the slide is that they agree, so agreement is asserted.

    Compared on the card-relevant columns rather than every column: the migrated
    schema descends from the real legacy shape, so it legitimately differs
    elsewhere. What must match is what the claim is about.
    """
    def payments_columns(schema):
        with both_paths.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = 'payments'",
                (schema,),
            )
            return {r[0].lower() for r in cur.fetchall()}

    fresh_cols = payments_columns(FRESH)
    migrated_cols = payments_columns(MIGRATED)
    assert fresh_cols, "no payments table on the fresh path"
    assert migrated_cols, "no payments table on the migrated path"

    for bad in FORBIDDEN_SUBSTRINGS:
        assert not any(bad in c for c in fresh_cols), f"fresh payments has {bad}"
        assert not any(bad in c for c in migrated_cols), f"migrated payments has {bad}"

    # last4 is what makes the removal survivable -- 0029 back-filled it so
    # payment history still renders. Both paths must have it, or the two
    # databases do not in fact agree on what replaced the PAN.
    assert "last4" in fresh_cols, "fresh payments has no last4"
    assert "last4" in migrated_cols, "migrated payments has no last4"
