"""Every seeded CIP row must carry the verdict its own factors imply.

`kyc_checks.cip_passed` (db/migrations/0033) is what the decision gate reads. A
NULL is a row that does not say, which the gate treats as not established -- so a
seeded row without it is an application that cannot be decided.

That failure is invisible in the obvious place. The migration back-fills existing
databases, so a MIGRATED database is fine and a FRESH one is not, and which you
have depends on whether someone ran `docker compose down -v`. A smoke test then
passes or fails according to how the volume was built, which is worse than
failing outright.

The expected value is computed from the row's own booleans and the applicant's
type rather than compared against a list kept here, so this cannot drift from
kyc-service's rule without one of the two being changed on purpose.
"""
import os

import psycopg2
import psycopg2.extras
import pytest

DATABASE_URL = os.getenv("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="needs DATABASE_URL")


def _rows():
    with psycopg2.connect(DATABASE_URL) as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT k.id, k.application_id, k.name_verified, k.dob_verified, "
                "       k.address_verified, k.ssn_verified, k.cip_passed, "
                "       COALESCE(a.is_entity, false) AS is_entity "
                "  FROM kyc_checks k "
                "  LEFT JOIN applicants a ON a.id = k.applicant_id "
                " WHERE k.application_id IS NOT NULL"
            )
            return cur.fetchall()


def _expected(row):
    """kyc-service's rule: an entity clears on name+address (D11); a natural
    person needs date of birth and SSN as well."""
    base = bool(row["name_verified"]) and bool(row["address_verified"])
    if row["is_entity"]:
        return base
    return base and bool(row["dob_verified"]) and bool(row["ssn_verified"])


def test_no_application_scoped_row_is_missing_its_verdict():
    missing = [r["id"] for r in _rows() if r["cip_passed"] is None]
    assert not missing, (
        f"kyc_checks rows {missing} carry an application_id but no cip_passed, so "
        f"the decision gate treats those applications as unverified. On a MIGRATED "
        f"database 0033 back-fills them and this passes; on a FRESH one it does "
        f"not -- the behaviour depends on how the volume was built."
    )


def test_every_verdict_matches_the_factors_it_was_computed_from():
    wrong = [
        (r["id"], r["cip_passed"], _expected(r))
        for r in _rows()
        if r["cip_passed"] is not None and r["cip_passed"] != _expected(r)
    ]
    assert not wrong, (
        f"rows (id, stored, expected) {wrong} record a verdict that disagrees with "
        f"their own factors under kyc-service's applicant-type rule"
    )


def test_the_check_actually_has_rows_to_check():
    """Guards the guard: an empty table would pass both tests above in silence."""
    rows = _rows()
    assert rows, "no application-scoped kyc_checks rows found -- the tests above proved nothing"
    assert any(r["is_entity"] for r in rows), (
        "no entity row in the fixture, so the entity branch of the rule is unexercised"
    )
