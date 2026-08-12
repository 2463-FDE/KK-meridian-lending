"""Neither a fresh database nor a migrated one has a PAN or CVV column.

Slide 2 of `docs/presentations/2026-08-12-three-slides.md` says the removal was
"verified on a fresh volume **and** a migrated one -- they agree". Everything it
cited proved half of that:

  - `db/migrations/0031_drop_payments_pan_cvv.sql` drops the columns from an
    EXISTING database;
  - `db/init/001_schema.sql` never creates them on a NEW one;
  - `test_expand_contract_pan_cvv.py` proves no card data survives the contract
    step on the legacy path.

None of them compares the two paths, which is exactly what the sentence claims.
A deck asserting that every claim resolves to evidence cannot itself carry a
claim whose evidence is "two separate facts that the reader is invited to
combine" -- the combination is the claim, and it was the part nobody checked.

So this builds both schemas from the real files and asserts the property on
each, plus their agreement. This file is the artifact Slide 2 cites.
"""
import os
from pathlib import Path

import psycopg2
import psycopg2.extras
import pytest

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against"
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
INIT_DIR = REPO_ROOT / "db" / "init"
MIGRATIONS_DIR = REPO_ROOT / "db" / "migrations"

INIT_SCHEMA_FILES = (
    "001_schema.sql", "004_decision_events.sql",
    "005_manual_reviews.sql", "006_decision_attempts.sql",
)

FRESH = "cardpath_fresh"
MIGRATED = "cardpath_migrated"

# The pre-tokenization shape, before ADR 0008. This is what a database that has
# been running since Week 4 actually contains, and it is the starting point the
# "migrated" half of the claim is about: columns that really hold a card number
# and a security code, which 0031 has to remove.
_LEGACY_PAYMENTS = """
CREATE TABLE payments (
    id          SERIAL PRIMARY KEY,
    loan_id     INTEGER,
    amount      NUMERIC(12,2),
    method      TEXT,
    pan         TEXT,
    cvv         TEXT,
    created_at  TIMESTAMPTZ DEFAULT now()
);
INSERT INTO payments (loan_id, amount, method, pan, cvv)
VALUES (1, 250.00, 'card', '4111111111111111', '123');
"""

# Any column that could hold a full card number or a security code. Substring
# matching, because `card_number`, `pan_encrypted` and `cvv2` are the same
# defect wearing a different name -- and a test that only looked for exactly
# "pan" and "cvv" would pass on a rename.
FORBIDDEN_SUBSTRINGS = ("pan", "cvv", "cvc", "card_number", "cardnumber", "security_code")

# Columns that contain a forbidden substring but are legitimate. `company_name`
# contains "pan"; so does `expansion`. Listed explicitly so the check can stay
# a substring match without becoming unusable.
ALLOWED_EXACT = {"company_name", "plan", "plan_id", "expansion", "span", "japan"}


def _run_sql(conn, schema, sql):
    with conn.cursor() as cur:
        cur.execute(f"SET search_path TO {schema}")
        # 0031 refuses to run without this: it destroys data and can break a
        # servicing instance still reading payments.pan. The harness IS the
        # operator here, so it acknowledges explicitly rather than the gate
        # being weakened to let automation through.
        cur.execute("SET meridian.pan_drop_acknowledged = 'yes'")
        cur.execute(sql)
    conn.commit()


def _has_executable_sql(sql: str) -> bool:
    stripped = "\n".join(
        line for line in sql.splitlines()
        if line.strip() and not line.strip().startswith("--")
    )
    return bool(stripped.strip())


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
    """Fresh volume, and a legacy database brought up to date."""
    for name in INIT_SCHEMA_FILES:
        _run_sql(conn, FRESH, (INIT_DIR / name).read_text(encoding="utf-8"))

    _run_sql(conn, MIGRATED, _LEGACY_PAYMENTS)
    for path in sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name):
        sql = path.read_text(encoding="utf-8")
        if not _has_executable_sql(sql):
            continue
        try:
            _run_sql(conn, MIGRATED, sql)
        except psycopg2.Error:
            # This schema starts from a hand-written legacy payments table, not
            # from the full init, so migrations touching unrelated tables have
            # nothing to apply to. The card columns are what this file is
            # about, and 0029/0031 operate on `payments`, which does exist.
            conn.rollback()
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


def test_a_migrated_database_has_no_card_columns(both_paths):
    """The legacy table really did have them, so this asserts a removal."""
    rows, found = _card_columns(both_paths, MIGRATED)
    assert rows, "the migrated schema has no columns at all -- it was never built"
    assert not found, f"card columns survive an upgrade: {found}"


def test_the_legacy_starting_point_really_had_card_columns(conn):
    """Guard the guard.

    Without this, `test_a_migrated_database_has_no_card_columns` would pass on a
    harness that silently failed to create the legacy table -- proving the
    removal works by never having anything to remove. That is the vacuous-pass
    trap, and this repository has already shipped it once.
    """
    _run_sql(conn, MIGRATED, _LEGACY_PAYMENTS)
    _, found = _card_columns(conn, MIGRATED)
    assert sorted(found) == ["payments.cvv", "payments.pan"], (
        f"the legacy fixture is not the pre-tokenization shape: {found}"
    )


def test_the_two_paths_agree_on_the_payments_table(both_paths):
    """The sentence on the slide is 'they agree', so agreement is the assertion.

    Compared on the card-relevant columns rather than the whole table: the
    migrated schema is built from a hand-written legacy payments table plus the
    migrations, so it legitimately lacks columns that only `db/init` creates.
    What must match is what the claim is about.
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

    # last4 is the column that makes the removal survivable -- 0029 back-filled
    # it so payment history still renders. Both paths must have it, or the two
    # databases do not in fact agree on what replaced the PAN.
    assert "last4" in fresh_cols, "fresh payments has no last4"
    assert "last4" in migrated_cols, "migrated payments has no last4"
