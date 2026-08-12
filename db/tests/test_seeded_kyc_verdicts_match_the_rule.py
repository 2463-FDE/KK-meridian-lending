"""Every seeded CIP row must carry the verdict its own factors imply.

`kyc_checks.cip_passed` (db/migrations/0033) is what the decision gate reads. A
NULL is a row that does not say, which the gate treats as not established -- so a
seeded row without one is an application that cannot be decided.

That failure hides in the worst possible place. The migration back-fills existing
databases, so a MIGRATED database is fine and a FRESH one is not, and which you
have depends on whether anyone ran `docker compose down -v`. A smoke test then
passes or fails according to how the volume was built, which is worse than one
that simply fails.

The expected value is computed from each row's own booleans and the applicant's
type rather than compared against a list kept here, so this cannot drift from
kyc-service's rule without one of the two being changed on purpose.

Built in a throwaway schema from db/init, the same way test_seed_offer_consistency
does, for the same reason: reading `public` would test whatever the last e2e run
left behind and would need a freshly seeded volume to mean anything. This tests
the seed DEFINITIONS, on any Postgres, including a CI job whose database is empty.
"""
import os
import pathlib

import psycopg2
import psycopg2.extras
import pytest

DATABASE_URL = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against"
)

SCHEMA = "seeded_kyc_verdicts"
INIT_DIR = pathlib.Path(__file__).resolve().parents[1] / "init"
INIT_FILES = (
    "001_schema.sql", "002_seed.sql", "003_seed_bulk.sql",
    "004_decision_events.sql", "005_manual_reviews.sql", "006_decision_attempts.sql",
)


@pytest.fixture(scope="module")
def seeded_rows():
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
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(f"SET search_path TO {SCHEMA}")
            cur.execute(
                "SELECT k.id, k.application_id, k.name_verified, k.dob_verified, "
                "       k.address_verified, k.ssn_verified, k.cip_passed, "
                "       COALESCE(a.is_entity, false) AS is_entity "
                "  FROM kyc_checks k "
                "  LEFT JOIN applicants a ON a.id = k.applicant_id "
                " WHERE k.application_id IS NOT NULL"
            )
            rows = cur.fetchall()
        conn.commit()
        yield rows
    finally:
        with conn.cursor() as cur:
            cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        conn.commit()
        conn.close()


def _expected(row):
    """kyc-service's rule: an entity clears on name + address (debt D11); a
    natural person needs a date of birth and an SSN as well."""
    base = bool(row["name_verified"]) and bool(row["address_verified"])
    if row["is_entity"]:
        return base
    return base and bool(row["dob_verified"]) and bool(row["ssn_verified"])


def test_no_application_scoped_row_is_missing_its_verdict(seeded_rows):
    missing = [r["application_id"] for r in seeded_rows if r["cip_passed"] is None]
    assert not missing, (
        f"seeded kyc_checks rows for applications {missing} carry no cip_passed, so "
        f"the decision gate treats those applications as unverified. On a MIGRATED "
        f"database 0033 back-fills them and this passes; on a FRESH one it does not "
        f"-- the behaviour depends on how the volume was built."
    )


def test_every_verdict_matches_the_factors_it_was_computed_from(seeded_rows):
    wrong = [
        (r["application_id"], r["cip_passed"], _expected(r))
        for r in seeded_rows
        if r["cip_passed"] is not None and r["cip_passed"] != _expected(r)
    ]
    assert not wrong, (
        f"rows (application_id, stored, expected) {wrong} record a verdict that "
        f"disagrees with their own factors under kyc-service's applicant-type rule"
    )


def test_the_check_actually_has_rows_to_check(seeded_rows):
    """Guards the guard: an empty result would satisfy both tests above in silence."""
    assert seeded_rows, (
        "no application-scoped kyc_checks rows in the seed -- the tests above "
        "proved nothing"
    )
    assert any(r["is_entity"] for r in seeded_rows), (
        "no entity row in the seed, so the entity branch of the rule is unexercised"
    )
    assert any(not r["is_entity"] for r in seeded_rows), (
        "no individual row in the seed, so the stricter branch is unexercised"
    )
