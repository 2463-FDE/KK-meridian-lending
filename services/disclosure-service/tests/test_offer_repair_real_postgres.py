"""PR #6 review -- migration 0026's documented remediation had to actually work.

0026 adds `offers_canonical_terms_present` NOT VALID and tells the operator to
"regenerate the offer from its decision (POST /offer is idempotent per
decision)". That instruction was wrong: `ON CONFLICT (decision_id) DO NOTHING`
followed by a read-back returns the SAME incomplete row, so the endpoint could
not regenerate anything -- and the float() coercions on the response turned the
NULL terms into a raw 500. Pre-0026 damage was therefore unfixable through the
API, by the only procedure the migration named.

POST /offers now repairs an incomplete row in place, and only under conditions
that cannot damage anything real:

  * an ACCEPTED offer is refused, never rewritten (the borrower is bound to it);
  * a COMPLETE offer is never touched -- the retry semantics are unchanged;
  * the decision must still be an approval at the instant of the write;
  * every repair writes an audit_logs row in the SAME statement as the update.

These need real Postgres: the repair is a single data-modifying CTE whose
correctness IS its WHERE clause and its atomicity with the audit insert. A
mocked cursor would assert nothing about either. Only the schema is a throwaway
-- app.db is the real module, pointed at a test schema.
"""
import os

import psycopg2
import psycopg2.extras
import pytest
from fastapi.testclient import TestClient

from app import config, db
from app.main import app

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against")

SCHEMA = "disclosure_repair_test"
client = TestClient(app)
AUTH = {"X-Internal-Token": config.INTERNAL_SERVICE_TOKEN}

# The five canonical TILA amounts, in disclosure order.
CANONICAL = ("apr", "finance_charge", "monthly_payment", "amount_financed", "total_of_payments")


def _schema_sql():
    """Mirrors db/init/001_schema.sql for the tables this endpoint touches.

    offers is created WITHOUT offers_canonical_terms_present on purpose: this
    file exists to test the repair of rows that predate that constraint, and on
    a real upgraded database 0026 adds it NOT VALID precisely so those rows
    survive to be repaired.
    """
    return f"""
        SET search_path TO {SCHEMA};
        CREATE TABLE applications (
            id SERIAL PRIMARY KEY,
            amount NUMERIC(14,2) NOT NULL,
            term_months INTEGER NOT NULL,
            status TEXT DEFAULT 'submitted'
        );
        CREATE TABLE decisions (
            app_id INTEGER PRIMARY KEY REFERENCES applications(id),
            outcome TEXT NOT NULL
        );
        CREATE TABLE offers (
            id SERIAL PRIMARY KEY,
            app_id INTEGER REFERENCES applications(id) UNIQUE,
            decision_id INTEGER REFERENCES decisions(app_id) UNIQUE,
            fee_pct_used NUMERIC(5,4),
            note_rate_pct NUMERIC(7,3),
            apr NUMERIC(7,3),
            finance_charge NUMERIC(14,2),
            monthly_payment NUMERIC(14,2),
            amount_financed NUMERIC(14,2),
            total_of_payments NUMERIC(14,2),
            created_at TIMESTAMPTZ DEFAULT now(),
            accepted_at TIMESTAMPTZ
        );
        CREATE TABLE audit_logs (
            id SERIAL PRIMARY KEY,
            actor TEXT,
            action TEXT,
            detail TEXT,
            deleted_at TIMESTAMPTZ,
            created_at TIMESTAMPTZ DEFAULT now()
        );
    """


@pytest.fixture
def pg(monkeypatch):
    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
        cur.execute(f"CREATE SCHEMA {SCHEMA}")
        cur.execute(_schema_sql())
    # The real app.db module, pointed at the throwaway schema.
    monkeypatch.setattr(db, "_conn", conn, raising=False)
    yield conn
    with conn.cursor() as cur:
        cur.execute(f"DROP SCHEMA IF EXISTS {SCHEMA} CASCADE")
    conn.close()


def _rows(conn, sql, params=()):
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(f"SET search_path TO {SCHEMA}")
        cur.execute(sql, params)
        return cur.fetchall() if cur.description else []


def _seed_approved_application(conn, app_id=1, amount=9000, term=24):
    _rows(conn, "INSERT INTO applications (id, amount, term_months) VALUES (%s, %s, %s)",
          (app_id, amount, term))
    _rows(conn, "INSERT INTO decisions (app_id, outcome) VALUES (%s, 'approve')", (app_id,))
    return app_id


def _seed_offer(conn, app_id, *, missing=(), accepted=False, fee_pct=0.030):
    """An offers row with `missing` canonical terms set to NULL."""
    values = {"apr": 5.946, "finance_charge": 768.11, "monthly_payment": 407.0,
              "amount_financed": 8730.0, "total_of_payments": 9768.11}
    for name in missing:
        values[name] = None
    _rows(
        conn,
        "INSERT INTO offers (app_id, decision_id, fee_pct_used, apr, finance_charge, "
        "monthly_payment, amount_financed, total_of_payments, accepted_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (app_id, app_id, fee_pct, values["apr"], values["finance_charge"],
         values["monthly_payment"], values["amount_financed"], values["total_of_payments"],
         "2026-01-01T00:00:00+00:00" if accepted else None),
    )
    return _rows(conn, "SELECT * FROM offers WHERE app_id = %s", (app_id,))[0]


def _post(app_id):
    # principal/term_months/annual_rate are still accepted by OfferIn but the
    # handler ignores them (they are sourced from the application's own record);
    # they are sent here only to satisfy the schema, as the other tests do.
    return client.post(
        "/offers",
        json={"application_id": app_id, "principal": 9000.0, "term_months": 24},
        headers=AUTH,
    )


# --- the defect this whole file is about --------------------------------------

def test_incomplete_unaccepted_offer_is_repaired_not_returned_incomplete(pg):
    """The reported blocker: DO NOTHING + read-back handed the incomplete row
    straight back, so 0026's named remediation could not work."""
    app_id = _seed_approved_application(pg)
    before = _seed_offer(pg, app_id, missing=("apr", "finance_charge"))
    assert before["apr"] is None

    resp = _post(app_id)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["repaired"] is True
    assert body["created"] is False
    assert body["apr"] is not None and body["finance_charge"] is not None

    after = _rows(pg, "SELECT * FROM offers WHERE app_id = %s", (app_id,))[0]
    assert [name for name in CANONICAL if after[name] is None] == []
    # Repaired in place: same offer id, still exactly one offer.
    assert after["id"] == before["id"]
    assert _rows(pg, "SELECT count(*) AS n FROM offers WHERE app_id = %s", (app_id,))[0]["n"] == 1


@pytest.mark.parametrize("missing", [(c,) for c in CANONICAL] + [CANONICAL])
def test_every_missing_term_combination_is_repaired(pg, missing):
    app_id = _seed_approved_application(pg)
    _seed_offer(pg, app_id, missing=missing)

    resp = _post(app_id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["repaired"] is True

    after = _rows(pg, "SELECT * FROM offers WHERE app_id = %s", (app_id,))[0]
    assert [name for name in CANONICAL if after[name] is None] == []


# --- immutability -------------------------------------------------------------

def test_accepted_incomplete_offer_is_refused_and_left_untouched(pg):
    """The borrower is already bound to an accepted offer. Even though its terms
    are unusable, rewriting them is worse than refusing."""
    app_id = _seed_approved_application(pg)
    before = _seed_offer(pg, app_id, missing=("apr",), accepted=True)

    resp = _post(app_id)
    assert resp.status_code == 409
    assert "already been accepted" in resp.json()["detail"]

    after = _rows(pg, "SELECT * FROM offers WHERE app_id = %s", (app_id,))[0]
    assert after["apr"] is None
    assert after["accepted_at"] == before["accepted_at"]
    assert _rows(pg, "SELECT count(*) AS n FROM audit_logs")[0]["n"] == 0


def test_complete_offer_is_never_rewritten_by_a_retry(pg):
    """Unchanged retry semantics: a repeat POST for a healthy offer returns the
    ORIGINAL terms and touches nothing -- the repair path must not widen this."""
    app_id = _seed_approved_application(pg)
    before = _seed_offer(pg, app_id, fee_pct=0.010)   # a deliberately unusual snapshot

    resp = _post(app_id)
    assert resp.status_code == 200
    assert resp.json()["repaired"] is False
    assert resp.json()["created"] is False
    assert resp.json()["fee_pct_used"] == pytest.approx(0.010)

    after = _rows(pg, "SELECT * FROM offers WHERE app_id = %s", (app_id,))[0]
    for name in CANONICAL:
        assert after[name] == before[name], f"{name} was rewritten by a retry"
    assert after["fee_pct_used"] == before["fee_pct_used"]
    assert _rows(pg, "SELECT count(*) AS n FROM audit_logs")[0]["n"] == 0


def test_repair_requires_the_decision_to_still_be_an_approval(pg):
    """Same standard the create path holds itself to: no offer -- and no repair
    of one -- for a decision that is not an approval right now."""
    app_id = _seed_approved_application(pg)
    _seed_offer(pg, app_id, missing=("apr",))
    _rows(pg, "UPDATE decisions SET outcome = 'deny' WHERE app_id = %s", (app_id,))

    resp = _post(app_id)
    assert resp.status_code == 409
    assert "no longer an approval" in resp.json()["detail"]

    after = _rows(pg, "SELECT * FROM offers WHERE app_id = %s", (app_id,))[0]
    assert after["apr"] is None
    assert _rows(pg, "SELECT count(*) AS n FROM audit_logs")[0]["n"] == 0


# --- auditability -------------------------------------------------------------

def test_every_repair_writes_one_audit_row_naming_what_was_missing(pg):
    app_id = _seed_approved_application(pg)
    _seed_offer(pg, app_id, missing=("apr", "total_of_payments"))

    assert _post(app_id).status_code == 200

    audit = _rows(pg, "SELECT actor, action, detail FROM audit_logs")
    assert len(audit) == 1
    assert audit[0]["actor"] == "disclosure-service"
    assert audit[0]["action"] == "offer.incomplete_terms_repaired"
    detail = audit[0]["detail"]
    assert f"app_id={app_id}" in detail
    assert "missing=apr,total_of_payments" in detail
    assert "fee_pct_used=" in detail


def test_a_second_post_after_a_repair_is_an_ordinary_no_op_retry(pg):
    """The repair happens once. The offer is complete afterwards, so the next
    call takes the normal read-back path and writes no second audit row."""
    app_id = _seed_approved_application(pg)
    _seed_offer(pg, app_id, missing=("apr",))

    first = _post(app_id)
    assert first.json()["repaired"] is True
    second = _post(app_id)
    assert second.status_code == 200
    assert second.json()["repaired"] is False
    assert second.json()["apr"] == pytest.approx(first.json()["apr"])

    assert _rows(pg, "SELECT count(*) AS n FROM audit_logs")[0]["n"] == 1


def test_audit_row_carries_no_applicant_identifiers(pg):
    """audit_logs is queryable by anyone with DB access. A repair record needs
    to identify the ROW, not the person."""
    app_id = _seed_approved_application(pg)
    _seed_offer(pg, app_id, missing=("monthly_payment",))
    assert _post(app_id).status_code == 200

    detail = _rows(pg, "SELECT detail FROM audit_logs")[0]["detail"]
    for token in ("ssn", "SSN", "@", "name="):
        assert token not in detail, f"audit detail leaked {token!r}: {detail}"
