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
            -- Model B schedule facts (db/migrations/0030). A repair must write
            -- these too, or it produces a row that displays fine and still
            -- cannot board.
            regular_payment_count INTEGER,
            final_payment NUMERIC(14,2),
            term_months INTEGER,
            schedule_version TEXT,
            -- The principal the schedule was calculated on (db/migrations/0030).
            principal NUMERIC(14,2),
            created_at TIMESTAMPTZ DEFAULT now(),
            accepted_at TIMESTAMPTZ
        );
        -- Present because the repair guard reads it: an offer with a loan has
        -- been boarded, whatever accepted_at says on an upgraded database.
        CREATE TABLE loans (
            id SERIAL PRIMARY KEY,
            app_id INTEGER UNIQUE,
            applicant_name TEXT,
            principal NUMERIC(14,2) NOT NULL,
            apr NUMERIC(7,3) NOT NULL,
            term_months INTEGER NOT NULL,
            status TEXT DEFAULT 'current',
            opened_at TIMESTAMPTZ DEFAULT now()
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


def _seed_offer(conn, app_id, *, missing=(), accepted=False, fee_pct=0.030,
                schedule=True):
    """An offers row with `missing` canonical terms set to NULL.

    `schedule=True` by default, so the row is COMPLETE -- five monetary amounts
    plus the Model B contractual terms. It used to write no schedule at all,
    which made every "healthy offer" fixture in this file quietly a legacy row.
    That mattered once schedule-only gaps began triggering repair: a test named
    "complete offer is never rewritten" was seeding an incomplete one.

    `schedule=False` is the legacy shape on purpose -- an unaccepted offer
    holding all five amounts and no stored schedule, which is exactly what every
    offer written before db/migrations/0030 looks like.
    """
    values = {"apr": 5.946, "finance_charge": 768.11, "monthly_payment": 407.0,
              "amount_financed": 8730.0, "total_of_payments": 9768.11}
    for name in missing:
        values[name] = None
    _rows(
        conn,
        "INSERT INTO offers (app_id, decision_id, fee_pct_used, note_rate_pct, apr, "
        "finance_charge, monthly_payment, amount_financed, total_of_payments, "
        "regular_payment_count, final_payment, term_months, schedule_version, principal, "
        "accepted_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (app_id, app_id, fee_pct,
         7.990 if schedule else None,
         values["apr"], values["finance_charge"],
         values["monthly_payment"], values["amount_financed"], values["total_of_payments"],
         23 if schedule else None,
         407.12 if schedule else None,
         24 if schedule else None,
         "B1" if schedule else None,
         # The principal the schedule was solved for. Part of a COMPLETE offer
         # now: boarding reads it, so an offer without it can neither board nor
         # be judged complete.
         9000.00 if schedule else None,
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


def test_a_repair_persists_every_model_b_schedule_field(pg):
    """A repaired unaccepted offer must come out fully boardable.

    Repairing the four-box amounts while leaving the schedule NULL would produce a
    row that displays correctly and still cannot board -- the half-fixed state
    BOARDING_REQUIRED_FIELDS exists to prevent. Every Model B field must be
    populated, and consistently: count + 1 == term.
    """
    app_id = _seed_approved_application(pg)
    _seed_offer(pg, app_id, missing=("apr", "finance_charge"))

    resp = _post(app_id)
    assert resp.status_code == 200, resp.text

    after = _rows(pg, "SELECT * FROM offers WHERE app_id = %s", (app_id,))[0]
    for field in ("note_rate_pct", "regular_payment_count", "final_payment",
                  "term_months", "schedule_version"):
        assert after[field] is not None, f"repair left {field} unset -- row cannot board"
    assert after["schedule_version"] == "B1"
    assert int(after["regular_payment_count"]) + 1 == int(after["term_months"])
    assert float(after["final_payment"]) > 0


def test_a_repair_never_writes_schedule_terms_into_an_accepted_offer(pg):
    """Accepted disclosures are immutable, including the new schedule columns.

    An accepted offer with NULL schedule fields is a legacy row whose contractual
    schedule was never recorded. Writing today's generated terms into it would
    persist invented terms as though they were the agreed ones.
    """
    app_id = _seed_approved_application(pg)
    # schedule=False is the shape this test is about: a legacy accepted row
    # whose contractual schedule was never recorded. Now explicit, because the
    # helper writes a complete schedule by default.
    _seed_offer(pg, app_id, missing=("apr",), accepted=True, schedule=False)

    cols = ("apr", "regular_payment_count", "final_payment", "term_months", "schedule_version")
    before = {c: _rows(pg, f"SELECT {c} FROM offers WHERE app_id = %s", (app_id,))[0][c]
              for c in cols}

    resp = _post(app_id)
    assert resp.status_code == 409, resp.text

    after = {c: _rows(pg, f"SELECT {c} FROM offers WHERE app_id = %s", (app_id,))[0][c]
             for c in cols}
    assert after == before, "an accepted offer was modified by a repair attempt"
    assert after["final_payment"] is None, "invented schedule terms were persisted"


# --- the shared offer projection, on every path that reads through it ---------
#
# Reviewed finding: _OFFER_COLUMNS omitted the four Model B schedule columns.
# They were written by the INSERT and then dropped on the way back out, so the
# borrower's disclosure reported no final payment on immediate creation -- the
# exact presentation defect this work removes, reintroduced one layer further
# out. One tuple feeds four statements, so the omission broke all four at once;
# these tests cover each separately, because a fix that repaired only one would
# otherwise look complete.

# What the DISCLOSURE carries. schedule_version is deliberately absent: it
# identifies the rounding policy that produced the row, which the SQL projection
# and the persisted row need but a borrower's disclosure does not.
_SCHEDULE_FIELDS = ("regular_payment_count", "final_payment", "term_months")
# What the ROW must carry, which is the projection's job.
_SCHEDULE_COLUMNS = _SCHEDULE_FIELDS + ("schedule_version",)


def _disclosure_of(resp):
    return resp.json()["disclosure"]


def test_immediate_creation_returns_the_schedule_it_just_wrote(pg):
    """Path 1: the RETURNING clause on the INSERT.

    This is the borrower's first sight of the offer, and the one that used to
    come back with a null final payment.
    """
    app_id = _seed_approved_application(pg)
    resp = _post(app_id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["created"] is True

    d = _disclosure_of(resp)
    for field in _SCHEDULE_FIELDS:
        assert d.get(field) is not None, f"creation returned no {field}"
    assert d["regular_payment_count"] + 1 == d["term_months"]
    # And it matches what was actually persisted, not merely something non-null.
    row = _rows(pg, "SELECT * FROM offers WHERE app_id = %s", (app_id,))[0]
    assert float(d["final_payment"]) == float(row["final_payment"])
    assert d["regular_payment_count"] == row["regular_payment_count"]


def test_the_idempotent_read_back_returns_the_schedule(pg):
    """Path 2: the second POST, which finds the existing row.

    A retry must be indistinguishable from the first call in what it reports --
    otherwise a borrower who refreshes sees a different offer.
    """
    app_id = _seed_approved_application(pg)
    first = _disclosure_of(_post(app_id))
    second_resp = _post(app_id)
    assert second_resp.json()["created"] is False
    second = _disclosure_of(second_resp)

    for field in _SCHEDULE_FIELDS:
        assert second.get(field) == first.get(field), f"{field} differs on retry"


def test_the_orm_model_maps_every_schedule_column(pg):
    """Path 3: the later GET.

    GET /applications/{id}/offer reads through SQLAlchemy rather than the SQL
    projection, so it needs the columns DECLARED on the model -- an undeclared
    column reads as None no matter what Postgres holds, which is the same
    failure this repository has now hit three times.

    Asserted as a mapping check rather than by calling the endpoint: the ORM
    engine binds to its own search_path and cannot see this fixture's schema, so
    an endpoint call here would fail for a reason unrelated to the projection.
    """
    from app import models

    mapped = set(models.Offer.__mapper__.columns.keys())
    missing = sorted(set(_SCHEDULE_COLUMNS) - mapped)
    assert not missing, f"models.Offer does not declare {missing}"


def test_a_repair_returns_the_schedule_it_wrote(pg):
    """Path 4: the repair statement's own RETURNING."""
    app_id = _seed_approved_application(pg)
    _seed_offer(pg, app_id, missing=("apr",), schedule=False)

    resp = _post(app_id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["repaired"] is True
    d = _disclosure_of(resp)
    for field in _SCHEDULE_FIELDS:
        assert d.get(field) is not None, f"repair returned no {field}"


# --- schedule-only legacy repair ---------------------------------------------
#
# Reviewed finding: the repair trigger tested missing_terms() alone -- the five
# monetary amounts. An unaccepted legacy offer holding all five and no stored
# schedule was therefore judged complete and left alone. It displayed perfectly
# and refused to board, with no route to fix it: the half-repaired state the
# boarding gate exists to expose, reached by never repairing at all.

def test_an_unaccepted_schedule_only_legacy_offer_is_regenerated(pg):
    """The finding itself. Five amounts present, no schedule -- must repair."""
    app_id = _seed_approved_application(pg)
    before = _seed_offer(pg, app_id, schedule=False)
    for name in CANONICAL:
        assert before[name] is not None, "fixture must have every monetary amount"
    assert before["final_payment"] is None

    resp = _post(app_id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["repaired"] is True, (
        "an offer with no contractual schedule was treated as complete"
    )

    after = _rows(pg, "SELECT * FROM offers WHERE app_id = %s", (app_id,))[0]
    for field in _SCHEDULE_COLUMNS:
        assert after[field] is not None, f"{field} was not regenerated"
    assert after["regular_payment_count"] + 1 == after["term_months"]


def test_the_schedule_only_repair_is_audited(pg):
    """Explicit regeneration, not a quiet patch. A schedule-only gap produces a
    new disclosure, and the audit row is what says so."""
    app_id = _seed_approved_application(pg)
    _seed_offer(pg, app_id, schedule=False)
    assert _rows(pg, "SELECT count(*) AS n FROM audit_logs")[0]["n"] == 0

    _post(app_id)

    logs = _rows(pg, "SELECT actor, action, detail FROM audit_logs")
    assert len(logs) == 1, "a regeneration happened with no audit record"
    assert logs[0]["action"] == "offer.incomplete_terms_repaired"
    # The audit names what was missing, so a reader can tell a schedule-only
    # regeneration from a full one.
    assert "final_payment" in logs[0]["detail"] or "schedule_version" in logs[0]["detail"]


def test_the_schedule_repair_and_its_audit_row_are_atomic(pg):
    """One data-modifying-CTE statement, so an unaudited repair cannot exist.

    Asserted by counting: if the UPDATE and the INSERT could commit separately,
    a repair with no audit row would be reachable. Here the two are the same
    statement, so the only two possible outcomes are both-or-neither.
    """
    app_id = _seed_approved_application(pg)
    _seed_offer(pg, app_id, schedule=False)

    _post(app_id)

    repaired = _rows(pg, "SELECT final_payment FROM offers WHERE app_id = %s", (app_id,))[0]
    audits = _rows(pg, "SELECT count(*) AS n FROM audit_logs")[0]["n"]
    assert (repaired["final_payment"] is not None) == (audits == 1), (
        "the repair and its audit row disagree -- they are not atomic"
    )


def test_an_accepted_schedule_only_legacy_offer_stays_immutable(pg):
    """The other half of the rule. An accepted offer binds the borrower to
    whatever it says, so a missing schedule is escalated, never invented."""
    app_id = _seed_approved_application(pg)
    _seed_offer(pg, app_id, schedule=False, accepted=True)

    resp = _post(app_id)
    assert resp.status_code == 409, resp.text

    after = _rows(pg, "SELECT * FROM offers WHERE app_id = %s", (app_id,))[0]
    for field in _SCHEDULE_COLUMNS:
        assert after[field] is None, f"{field} was invented on an accepted offer"
    assert _rows(pg, "SELECT count(*) AS n FROM audit_logs")[0]["n"] == 0


def test_a_fully_complete_offer_is_still_never_repaired(pg):
    """The boundary. Widening the trigger must not make every retry a rewrite."""
    app_id = _seed_approved_application(pg)
    before = _seed_offer(pg, app_id, fee_pct=0.010)

    resp = _post(app_id)
    assert resp.json()["repaired"] is False

    after = _rows(pg, "SELECT * FROM offers WHERE app_id = %s", (app_id,))[0]
    for field in CANONICAL + _SCHEDULE_COLUMNS:
        assert after[field] == before[field], f"{field} was rewritten"
    assert _rows(pg, "SELECT count(*) AS n FROM audit_logs")[0]["n"] == 0


# --- an offer that has already been boarded is not repairable ----------------

def _board(conn, app_id, *, principal=9000, rate=7.99, term=24):
    """A loan for this application, with accepted_at left NULL on the offer.

    That combination is not hypothetical: migration 0021 added accepted_at
    without back-filling, so every offer boarded before it looks exactly like
    this on an upgraded database.
    """
    _rows(
        conn,
        "INSERT INTO loans (app_id, applicant_name, principal, apr, term_months) "
        "VALUES (%s, 'Boarded Borrower', %s, %s, %s)",
        (app_id, principal, rate, term),
    )


def test_a_boarded_offer_is_not_rewritten_even_with_accepted_at_null(pg):
    """The reviewed hole, in the shape an upgraded database actually produces.

    A pre-0021 boarded offer has a loan and a NULL accepted_at, and 0030 leaves
    its schedule columns NULL by design -- which is precisely the shape the
    widened schedule-only repair now accepts. Without a loan check, an
    authorised POST /offers retry rewrites every monetary and contractual term
    of an offer somebody has already been funded against.
    """
    app_id = _seed_approved_application(pg, app_id=71)
    before = _seed_offer(pg, app_id, schedule=False)
    _board(pg, app_id)

    resp = _post(app_id)

    after = _rows(pg, "SELECT * FROM offers WHERE app_id = %s", (app_id,))[0]
    for field in ("apr", "finance_charge", "monthly_payment", "amount_financed",
                  "total_of_payments", "fee_pct_used"):
        assert after[field] == before[field], (
            f"{field} was rewritten on an offer that has already been boarded"
        )
    # The response still answers -- refusing to REPAIR is not refusing to READ.
    # What it must not do is hand back terms it just invented.
    assert resp.status_code in (200, 409), resp.text
    if resp.status_code == 200:
        assert resp.json()["apr"] == float(before["apr"])
    # No repair was audited, because none happened.
    audits = _rows(pg, "SELECT * FROM audit_logs WHERE action = 'offer.incomplete_terms_repaired'")
    assert audits == []


def test_an_unboarded_legacy_offer_is_still_repairable(pg):
    """The other side of the guard: no loan means the repair path still works.

    Narrowing the boundary must not re-break the schedule-only repair that the
    previous round added -- an unaccepted, unboarded pre-0030 row is exactly
    what it exists for.
    """
    app_id = _seed_approved_application(pg, app_id=72)
    _seed_offer(pg, app_id, schedule=False)

    resp = _post(app_id)

    assert resp.status_code == 200, resp.text
    after = _rows(pg, "SELECT * FROM offers WHERE app_id = %s", (app_id,))[0]
    assert after["final_payment"] is not None
    assert after["note_rate_pct"] is not None
    assert after["principal"] is not None, "the repair must store the principal too"
    audits = _rows(pg, "SELECT * FROM audit_logs WHERE action = 'offer.incomplete_terms_repaired'")
    assert len(audits) == 1


def test_an_idempotent_post_returns_the_stored_schedule_not_a_regenerated_one(pg):
    """The rows must describe the row being RETURNED.

    On the idempotent path POST finds an existing offer and returns its stored
    payment-plan fields -- but the detailed `schedule` was still the one
    generated from the application's current inputs with the currently deployed
    generator. So after a generator change, or after the application's amount or
    term was corrected, the same response advertised the stored regular/final
    payments beside rows carrying different ones. Reviewed on PR #10.

    Simulated by moving the application out from under the stored offer, which
    is the shape a corrected amount produces.
    """
    app_id = _seed_approved_application(pg, app_id=81, amount=9000, term=24)
    _seed_offer(pg, app_id)   # complete: 9,000 over 24 at 7.99, final 407.12

    # The application is corrected AFTER the offer was written.
    _rows(pg, "UPDATE applications SET amount = 12000 WHERE id = %s", (app_id,))

    resp = _post(app_id)
    assert resp.status_code == 200, resp.text
    body = resp.json()

    stored = _rows(pg, "SELECT monthly_payment, final_payment, term_months FROM offers "
                       "WHERE app_id = %s", (app_id,))[0]
    rows = body["schedule"]
    assert len(rows) == int(stored["term_months"])
    assert rows[-1]["payment"] == pytest.approx(float(stored["final_payment"]), abs=0.005)
    assert all(
        r["payment"] == pytest.approx(float(stored["monthly_payment"]), abs=0.005)
        for r in rows[:-1]
    ), "the returned rows were generated from the corrected application, not the stored offer"
    # And the summary fields agree with the rows they are printed beside.
    assert body["disclosure"]["final_payment"] == pytest.approx(rows[-1]["payment"], abs=0.005)


def test_an_amount_too_small_for_its_term_is_refused_not_a_500(pg):
    """A principal that cannot produce a schedule must fail cleanly.

    Cent-rounded regular payments can exhaust the balance before the last
    period: $0.10 over 12 months gives eleven $0.01 payments and a final of
    -$0.01. The INSERT then violated offers_final_payment_positive and the
    caller saw an internal error, with an approved application that could never
    obtain an offer. `ApplicationIn` permits amounts below the UI's $1,000
    slider minimum, so this is reachable. Reviewed on PR #10.
    """
    app_id = _seed_approved_application(pg, app_id=61, amount=0.10, term=12)

    resp = _post(app_id)

    assert resp.status_code == 422, resp.text
    assert "payment schedule" in resp.json()["detail"]
    # And nothing was written: a refused offer must not leave a partial row.
    assert _rows(pg, "SELECT * FROM offers WHERE app_id = %s", (app_id,)) == []


def test_a_normal_amount_still_creates_an_offer(pg):
    """The guard must not refuse an ordinary application."""
    app_id = _seed_approved_application(pg, app_id=62, amount=9000, term=24)
    resp = _post(app_id)
    assert resp.status_code == 200, resp.text
    assert resp.json()["disclosure"]["final_payment"] > 0


# --- legacy offer whose decision_id was never set -----------------------------
#
# Reviewed on PR #10. `offers.decision_id` and `offers.app_id` are two separate
# UNIQUE constraints, and the INSERT sets both -- but rows written before
# migration 0011 have `app_id` populated and `decision_id` NULL. For those the
# INSERT collides on `offers_app_id_key`, which the ON CONFLICT clause does not
# target, so it raises UniqueViolation; the fallback then read the offer back
# `WHERE decision_id = %s`, found nothing, and reported "no approved decision on
# record" for an application that has both a decision and an offer.

def _seed_offer_without_decision_id(conn, app_id, *, accepted=False, schedule=False):
    """The pre-0011 shape: app_id set, decision_id NULL."""
    _rows(
        conn,
        "INSERT INTO offers (app_id, decision_id, fee_pct_used, apr, finance_charge, "
        "monthly_payment, amount_financed, total_of_payments, accepted_at) "
        "VALUES (%s, NULL, 0.030, 5.946, 768.11, 407.0, 8730.0, 9768.11, %s)",
        (app_id, "2026-01-01T00:00:00+00:00" if accepted else None),
    )
    return _rows(conn, "SELECT * FROM offers WHERE app_id = %s", (app_id,))[0]


def test_a_legacy_offer_with_a_null_decision_id_converges_to_one_boardable_offer(pg):
    """The reported blocker, end to end.

    Approved decision + existing unaccepted offer + matching app_id +
    decision_id NULL. A regenerate must find that offer by app_id, adopt it,
    stamp its decision_id from the decision it belongs to, and return ONE
    complete boardable offer -- not a 422 claiming no decision exists, and not a
    second offer row.
    """
    app_id = _seed_approved_application(pg)
    before = _seed_offer_without_decision_id(pg, app_id)
    assert before["decision_id"] is None
    assert before["schedule_version"] is None

    resp = _post(app_id)

    assert resp.status_code == 200, resp.text
    rows = _rows(pg, "SELECT * FROM offers WHERE app_id = %s", (app_id,))
    assert len(rows) == 1, "a second offer row was created for the same application"
    row = rows[0]
    assert row["id"] == before["id"], "the existing offer was replaced instead of repaired"
    assert row["decision_id"] == app_id, "decision_id was left NULL"
    # Boardable: every contractual fact present.
    for field in ("principal", "note_rate_pct", "monthly_payment",
                  "regular_payment_count", "final_payment", "term_months",
                  "schedule_version"):
        assert row[field] is not None, f"{field} still missing -- offer cannot board"


def test_an_accepted_legacy_offer_with_a_null_decision_id_is_not_rewritten(pg):
    """Adopting the row must not become a licence to change agreed terms.

    Stamping `decision_id` is bookkeeping -- it records which decision the offer
    already belonged to. Regenerating the money is not, and an accepted offer's
    terms are immutable. So this asserts the monetary columns are byte-identical
    afterwards, whatever the endpoint decides to do with the request.
    """
    app_id = _seed_approved_application(pg)
    before = _seed_offer_without_decision_id(pg, app_id, accepted=True)

    _post(app_id)

    after = _rows(pg, "SELECT * FROM offers WHERE app_id = %s", (app_id,))[0]
    assert len(_rows(pg, "SELECT * FROM offers WHERE app_id = %s", (app_id,))) == 1
    for field in ("apr", "finance_charge", "monthly_payment", "amount_financed",
                  "total_of_payments", "fee_pct_used", "accepted_at"):
        assert after[field] == before[field], f"{field} changed on an accepted offer"
