"""Reading account activity cannot change the reconciliation result.

Account activity reads `ledger_entries`. Reconciliation reads `payments` --
specifically `processor_ref`, `captured_at`, `capture_source` and `auth_status` --
against the settlement file. The two share a database and nothing else, and this
file exists to hold that separation to account rather than assert it in a
docstring.

**The proof is an A/B comparison against the same fixture**: run the comparison,
read the activity endpoint, run the comparison again, and require the two results
to be identical. A read that mutated a column reconciliation keys on, opened a
transaction that changed visibility, or wrote a row would show up as a
difference. Nothing weaker demonstrates it: a test that only checked the columns
exist would pass while a read path quietly UPDATEd one.

The fixture is the reported netting scenario, reused deliberately -- it is the
one where a regression is most expensive, because per-loan totals agree while two
real defects sit underneath.
"""
import os
import pathlib
from decimal import Decimal

import psycopg2
import psycopg2.extras
import pytest
from fastapi.testclient import TestClient

DATABASE_URL = os.environ.get("DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL, reason="DATABASE_URL not set -- no Postgres to test against"
)

REPO = pathlib.Path(__file__).resolve().parents[3]
SCHEMA = "activity_vs_reconciliation_test"
SETTLEMENT_DAY = "2026-08-09"

#: The netting case: the file settles PR-100231 at 150.01 (a 250.00 capture less
#: a 99.99 refund we never recorded) and carries a PR-100244 capture we hold no
#: payment for. Per loan both sides total 250.00.
SETTLEMENT_ROWS = [
    ("PR-100231", "250.00", "capture"),
    ("PR-100244", "99.99", "capture"),
    ("PR-100231", "99.99", "refund"),
]


@pytest.fixture
def db(monkeypatch):
    from app import reconciliation

    conn = psycopg2.connect(DATABASE_URL)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS %s CASCADE" % SCHEMA)
        cur.execute("CREATE SCHEMA %s" % SCHEMA)
        cur.execute("SET search_path TO %s" % SCHEMA)
        cur.execute((REPO / "db" / "init" / "001_schema.sql").read_text(encoding="utf-8"))
        cur.execute(
            "INSERT INTO loans (id, applicant_name, principal, note_rate_pct, "
            "term_months) VALUES (4471, 'Sam Okafor', 9000, 5.946, 24)")
        cur.execute("INSERT INTO balances (loan_id, balance, past_due) "
                    "VALUES (4471, 8750.00, 25.00)")
        cur.execute(
            "INSERT INTO payments (loan_id, amount, method, auth_status, "
            "captured_at, processor_ref, capture_source) "
            "VALUES (4471, 250.00, 'card', 'captured', %s::date + TIME '10:00', "
            "'PR-100231', 'processor') RETURNING id",
            (SETTLEMENT_DAY,))
        payment_id = cur.fetchone()[0]

    # Everything else in ONE transaction, and the database dictated that shape
    # through three separate refusals. Each is a control this repository built,
    # and a fixture able to sidestep any of them would be exercising a schema
    # nobody deploys:
    #
    #   1. `ledger_payment_allocation_exact` (CONSTRAINT TRIGGER ... DEFERRABLE
    #      INITIALLY DEFERRED) requires a payment's ledger rows to sum to the
    #      captured amount. Under autocommit the first row committed alone and was
    #      refused for allocating 25.00 against a 250.00 capture -- a partial
    #      allocation is never a valid state.
    #   2. `ledger_entry_matches_its_proposal` refused a bare adjustment row: "a
    #      adjustment entry must name the proposal that authorised it", and then
    #      requires that proposal to be APPROVED by someone other than the
    #      requester, on the same loan, component, amount and type. Hence a
    #      proposal raised by user 7 and approved by user 9.
    #   3. `pending_movement_resolution_is_complete` refused the approved
    #      proposal until its `ledger_entry_id` pointed at the entry. The link is
    #      bidirectional on purpose: an approval with no entry, or an entry with
    #      no approval, are both missing halves of the same record.
    conn.autocommit = False
    with conn.cursor() as cur:
        cur.execute("SET search_path TO %s" % SCHEMA)

        # The payment's allocation: fees + interest + principal = the 250.00
        # captured, so activity has a real multi-row payment to group.
        for component, amount in (("fees", "-25.00"), ("interest", "-75.00"),
                                  ("principal", "-150.00")):
            cur.execute(
                "INSERT INTO ledger_entries (loan_id, component, amount, "
                "entry_type, payment_id, occurred_at) VALUES "
                "(4471, %s, %s, 'payment', %s, %s::date + TIME '10:00')",
                (component, amount, payment_id, SETTLEMENT_DAY))

        # An approved +450 principal adjustment: a movement with no payment
        # behind it. It must appear in activity and must never appear to
        # reconciliation, because no processor money moved.
        cur.execute(
            "INSERT INTO pending_movements (loan_id, component, amount, "
            "entry_type, reason, requested_by, requested_role, resolution, "
            "resolved_by, resolved_role, resolved_at, resolved_threshold) VALUES "
            "(4471, 'principal', 450.00, 'adjustment', 'internal ops note', 7, "
            "'csr', 'approved', 9, 'admin', now(), 500.00) RETURNING id")
        movement_id = cur.fetchone()[0]
        # `actor_id`/`actor_role` are deliberately NOT supplied: the trigger
        # overwrites them with the APPROVER, so a caller reproducing everything
        # else correctly still cannot choose who is credited with authorising it.
        cur.execute(
            "INSERT INTO ledger_entries (loan_id, component, amount, entry_type, "
            "reason, pending_movement_id, occurred_at) VALUES "
            "(4471, 'principal', 450.00, 'adjustment', 'internal ops note', %s, "
            "%s::date + TIME '11:00') RETURNING id",
            (movement_id, SETTLEMENT_DAY))
        entry_id = cur.fetchone()[0]
        cur.execute("UPDATE pending_movements SET ledger_entry_id = %s WHERE id = %s",
                    (entry_id, movement_id))
    conn.commit()
    conn.autocommit = True

    scoped = "%s?options=-csearch_path%%3D%s" % (DATABASE_URL, SCHEMA)
    monkeypatch.setattr(reconciliation.db, "DATABASE_URL", scoped, raising=False)
    monkeypatch.setattr(reconciliation.db, "_conn", None, raising=False)
    yield conn
    with conn.cursor() as cur:
        cur.execute("DROP SCHEMA IF EXISTS %s CASCADE" % SCHEMA)
    conn.close()
    monkeypatch.setattr(reconciliation.db, "_conn", None, raising=False)


@pytest.fixture
def settlement(tmp_path, monkeypatch):
    from app import reconciliation

    path = tmp_path / "settlement.csv"
    lines = ["settlement_date,processor_ref,loan_id,amount,type"]
    lines += ["%s,%s,4471,%s,%s" % (SETTLEMENT_DAY, ref, amount, kind)
              for ref, amount, kind in SETTLEMENT_ROWS]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setattr(reconciliation, "SETTLEMENT_FILE", str(path), raising=False)
    return str(path)


def _activity(monkeypatch):
    """The activity endpoint, against the same schema, through the real route."""
    from app import main
    from app.database import get_session
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session as OrmSession

    engine = create_engine(
        "postgresql+psycopg2://%s" % DATABASE_URL.split("://", 1)[1],
        connect_args={"options": "-csearch_path=%s" % SCHEMA})

    def _scoped_session():
        with OrmSession(engine) as session:
            yield session

    main.app.dependency_overrides[get_session] = _scoped_session
    try:
        response = TestClient(main.app).get("/loans/4471/activity")
    finally:
        main.app.dependency_overrides.pop(get_session, None)
        engine.dispose()
    assert response.status_code == 200, response.text
    return response.json()


def _comparable(result: dict) -> dict:
    """The parts of a comparison that must not move. Excludes nothing that
    matters: counts, totals, the break list and its value."""
    return {
        "loans_compared": result["loans_compared"],
        "references_compared": result["references_compared"],
        "unreferenced_captures": result["unreferenced_captures"],
        "out_of_scope_captures": result["out_of_scope_captures"],
        "breaks_found": len(result["breaks"]),
        "break_value": str(result["break_value"]),
        "breaks": sorted(str(sorted(b.items())) for b in result["breaks"]),
    }


def test_reading_activity_leaves_the_comparison_identical(db, settlement, monkeypatch):
    """A, then the read, then B. The whole point of the file."""
    from app import reconciliation

    before = _comparable(reconciliation.compare())

    body = _activity(monkeypatch)
    assert body["items"], "the fixture produced no activity, so this proves nothing"

    after = _comparable(reconciliation.compare())

    assert before == after, (
        "reading account activity changed the reconciliation result:\n"
        "before=%r\nafter=%r" % (before, after))


def test_the_fixture_really_is_the_netting_case(db, settlement):
    """Guard the guard. If the comparison found nothing, the A/B equality above
    would hold trivially -- two clean runs are equal for the wrong reason."""
    from app import reconciliation

    result = reconciliation.compare()

    assert len(result["breaks"]) == 2, result["breaks"]
    assert result["break_value"] > Decimal("0")


def test_activity_does_not_alter_the_payment_columns_reconciliation_keys_on(
        db, settlement, monkeypatch):
    """Column-level, because an A/B equality could in principle hold while a
    value moved and moved back."""
    def _snapshot():
        with db.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("SET search_path TO %s" % SCHEMA)
            cur.execute("SELECT id, processor_ref, captured_at, capture_source, "
                        "auth_status, amount FROM payments ORDER BY id")
            return cur.fetchall()

    before = _snapshot()
    _activity(monkeypatch)

    assert _snapshot() == before, (
        "reading activity changed a payments column reconciliation depends on")


def test_activity_writes_no_ledger_row(db, monkeypatch):
    """The immutable side. A read model that appended would corrupt the record it
    reports on."""
    def _count():
        with db.cursor() as cur:
            cur.execute("SET search_path TO %s" % SCHEMA)
            cur.execute("SELECT count(*) FROM ledger_entries")
            return cur.fetchone()[0]

    before = _count()
    _activity(monkeypatch)

    assert _count() == before


def test_an_approved_adjustment_is_activity_but_not_a_capture(db, settlement,
                                                              monkeypatch):
    """The distinction §26 turns on: an approved adjustment moves the account and
    creates no processor capture, because no processor money moved. It must be
    visible in activity and invisible to the settlement comparison."""
    from app import reconciliation

    body = _activity(monkeypatch)
    adjustments = [item for item in body["items"] if item["category"] == "adjustment"]
    assert len(adjustments) == 1, body["items"]
    assert adjustments[0]["amount"] == 450.00

    result = reconciliation.compare()
    # Two references on the settlement side; the adjustment added neither a
    # reference nor a capture.
    assert result["references_compared"] == 2
    assert result["unreferenced_captures"] == 0


def test_the_adjustment_reason_and_actor_stay_out_of_the_response(db, monkeypatch):
    """The fixture stores a reason and an actor deliberately. A guard asserting
    their absence against rows that never had them proves nothing."""
    body = _activity(monkeypatch)

    flat = repr(body)
    assert "internal ops note" not in flat
    assert "admin" not in flat
