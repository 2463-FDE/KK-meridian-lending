"""README's claims about the payments schema must match the payments schema.

The README said `payments.pan` and `payments.cvv` were "still there ... waiting
to be dropped" for two days after `db/migrations/0031` dropped them. Nobody was
careless: the migration and the sentence live in different files, and only one of
them is executed.

That is the drift this checks. It reads the claim out of the README and the truth
out of the schema, and fails when they disagree — in EITHER direction. A README
that says the columns are gone while they are back is the more dangerous of the
two, because it is a card-data claim on a lending system.

Built in a throwaway schema from `db/init`, the same pattern as
`test_seed_offer_consistency.py`: reading `public` would test whatever the last
e2e run left behind and would need a freshly seeded volume to mean anything. This
tests the schema DEFINITION on any Postgres, including a CI job whose database
starts empty.
"""
import os
import pathlib
import re

import psycopg2
import pytest

DATABASE_URL = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against"
)

REPO = pathlib.Path(__file__).resolve().parents[2]
README = REPO / "README.md"
SCHEMA = "readme_schema_claims"
INIT_DIR = REPO / "db" / "init"
INIT_FILES = (
    "001_schema.sql", "002_seed.sql", "003_seed_bulk.sql",
    "004_decision_events.sql", "005_manual_reviews.sql", "006_decision_attempts.sql",
)

#: Columns the README makes an explicit existence claim about. Card data only --
#: this is not a general schema-doc checker, it is the claim that has been wrong.
CARD_COLUMNS = ("pan", "cvv")


@pytest.fixture(scope="module")
def payments_columns():
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
            cur.execute(f"CREATE SCHEMA {SCHEMA}")
        conn.commit()
        for name in INIT_FILES:
            path = INIT_DIR / name
            if not path.exists():
                continue
            with conn.cursor() as cur:
                cur.execute(f"SET search_path TO {SCHEMA}")
                cur.execute(path.read_text())
            conn.commit()
        with conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_schema = %s AND table_name = 'payments'", (SCHEMA,))
            cols = {r[0] for r in cur.fetchall()}
        conn.commit()
        yield cols
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.commit()
        conn.close()


def _readme() -> str:
    return README.read_text(encoding="utf-8")


def test_the_fixture_built_a_payments_table(payments_columns):
    """Guards the guard: an empty set would satisfy every absence check below."""
    assert payments_columns, "db/init produced no payments table -- nothing was checked"
    assert "last4" in payments_columns, (
        "payments has no last4 column, so the schema this test read is not the one "
        "the README describes"
    )


@pytest.mark.parametrize("column", CARD_COLUMNS)
def test_the_card_columns_really_are_gone(payments_columns, column):
    assert column not in payments_columns, (
        f"payments.{column} exists in the fresh schema. The README states card data "
        f"is not stored -- fix the schema, or the README is making a false card-data "
        f"claim on a lending system."
    )


@pytest.mark.parametrize("column", CARD_COLUMNS)
def test_the_readme_does_not_claim_a_dropped_column_still_exists(payments_columns, column):
    """The direction that actually happened.

    Matches present-tense existence claims, not history. The README is allowed --
    encouraged — to say the columns USED to exist and were dropped; what it may
    not do is say they are there now.
    """
    if column in payments_columns:                       # covered by the test above
        return
    text = _readme()
    stale = re.findall(
        rf"(?im)^.*\b(?:still (?:carries|has|there)|waiting to be dropped)\b.*"
        rf"`?payments\.?{column}`?.*$", text)
    stale += re.findall(
        rf"(?im)^.*`payments\.{column}`.*\b(?:still|remains?|are there)\b.*$", text)
    # Anything inside the italic history note is a record of the old claim, not a claim.
    stale = [s for s in stale if "previously said" not in s]
    assert not stale, (
        f"README asserts payments.{column} still exists, but the schema has no such "
        f"column: {stale}"
    )


def test_the_readme_does_not_claim_pci_compliance():
    """Removing stored card data closes a violation. It is not an assessment.

    There is no QSA, no real processor and no scoped cardholder-data environment
    here, so any sentence reading as a compliance claim is false regardless of how
    good the schema is.
    """
    text = _readme()
    claims = re.findall(
        r"(?im)^.*\b(?:is|are|now)\s+PCI[- ]DSS\s+compliant\b.*$", text)
    claims = [c for c in claims if not re.search(r"(?i)\bnot\s+PCI[- ]DSS\s+compliant", c)]
    claims = [c for c in claims if "as false" not in c]
    assert not claims, f"README claims PCI-DSS compliance: {claims}"


def test_the_readme_still_states_what_is_retained():
    """The absence claim is only useful next to the presence claim."""
    text = _readme().lower()
    assert "last4" in text and "brand" in text, (
        "README no longer says what the payments row DOES keep, so 'no card data' "
        "is unanchored"
    )
