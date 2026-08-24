"""Signals are recorded around the money controls, never through them.

The client's decision of 2026-08-24 authorised flagging payments for human
review. It authorised nothing else: a flag is not a duplicate conclusion, not a
validity conclusion, and not permission to move money. This file holds the half
of that contract a unit test of the predicate cannot reach -- what `charge()`
actually does when a signal is raised.

Two properties matter more than the signals themselves:

1. **The existing controls are untouched.** `payments.idempotency_key` still has
   its partial unique index and `charge()` still inserts `ON CONFLICT DO
   NOTHING`; a repeated key still replays the original result without a second
   authorization. No second `payments` row is created so that a review item has
   something to point at -- weakening idempotency to fill a queue would be worse
   than having no queue.
2. **Recording a signal cannot fail a payment.** The queue is an observation
   about the money path, not part of it, so a review-table outage must not turn a
   reporting feature into an availability incident on capture.
"""
import datetime
from decimal import Decimal

import pytest
from psycopg2.errors import UniqueViolation
from fastapi.testclient import TestClient

from app import config, payments, processor, review_signals
from app.main import app

client = TestClient(app)
client.headers.update({"X-Internal-Token": config.INTERNAL_SERVICE_TOKEN})

VALID_MOCK_TOKEN = "tok_mock_550e8400-e29b-41d4-a716-446655440000"
SOURCE_REF = "src_mock_11111111-1111-4111-8111-111111111111"


class _Ok:
    status_code = 200

    def raise_for_status(self):
        pass


class _Db:
    """A payments table that honours the unique idempotency key, and records
    every statement so the test can ask what the capture path actually did."""

    def __init__(self, review_insert_fails=False):
        self.calls = []
        self.review_inserts = []
        self._next_id = 1
        self._by_key = {}
        self._by_id = {}
        self.review_insert_fails = review_insert_fails

    def query(self, sql, params=None):
        flat = " ".join(sql.split())
        self.calls.append((flat, params))

        if "reconciliation_review_items" in flat:
            if flat.startswith("INSERT"):
                if self.review_insert_fails:
                    raise RuntimeError("review table unavailable")
                self.review_inserts.append(params)
                return [{"id": len(self.review_inserts)}]
            return []

        if flat.startswith("INSERT INTO payments"):
            (loan_id, last4, brand, amount, method, key, correlation_id,
             source_ref) = params
            if key is not None and key in self._by_key:
                return []                       # ON CONFLICT DO NOTHING
            row = {"id": self._next_id, "loan_id": loan_id,
                   "amount": Decimal(str(amount)), "last4": last4, "brand": brand,
                   "method": method, "applied_at": None, "auth_status": "pending",
                   "correlation_id": correlation_id, "source_ref": source_ref,
                   "captured_at": None, "processor_ref": None}
            self._next_id += 1
            if key is not None:
                self._by_key[key] = row
            self._by_id[row["id"]] = row
            return [row]

        if flat.startswith("SELECT 1 FROM payments WHERE processor_ref"):
            ref, pid = params
            return [{"?column?": 1} for row in self._by_id.values()
                    if row.get("processor_ref") == ref and row["id"] != pid]

        if flat.startswith("SELECT id FROM payments WHERE processor_ref"):
            ref, pid = params
            return [{"id": row["id"]} for row in self._by_id.values()
                    if row.get("processor_ref") == ref and row["id"] != pid][:1]

        if "FROM payments WHERE loan_id" in flat and "source_ref" in flat:
            loan_id, source_ref, method, since, pid = params
            return [row for row in self._by_id.values()
                    if row["loan_id"] == loan_id and row["source_ref"] == source_ref
                    and row["method"] == method and row["id"] != pid
                    and row["captured_at"] is not None and row["captured_at"] >= since]

        if flat.startswith("SELECT id, loan_id, amount, method, source_ref, captured_at"):
            (pid,) = params
            row = self._by_id.get(pid)
            return [row] if row else []

        if flat.startswith("SELECT"):
            if params and len(params) == 1 and isinstance(params[0], str):
                row = self._by_key.get(params[0])
                return [row] if row else []
            return []

        if flat.startswith("UPDATE payments"):
            if "auth_status = 'captured'" in flat:
                # Two capture statements exist: the normal one, and the fallback
                # that omits `processor_ref` when the reference belongs to
                # another row (payments.py::_mark_captured). The fake handles
                # both, because a fake that only knew the happy shape would fail
                # on the very path this file was extended to cover.
                if "processor_ref = %s" in flat:
                    auth_id, captured_at, ref, pid = params
                else:
                    auth_id, captured_at, pid = params
                    ref = None
                # The real statement is `COALESCE(%s::timestamptz, now())`: the
                # stub processor reports no timestamp, so production stamps the
                # local clock. Emulated here rather than storing NULL, because a
                # fake that leaves `captured_at` empty silently disables the
                # window the heuristic is built on -- which looked exactly like
                # the feature not working.
                self._by_id[pid].update(
                    auth_status="captured", authorization_id=auth_id,
                    captured_at=captured_at or datetime.datetime.now(
                        datetime.timezone.utc),
                    processor_ref=ref)
            elif "auth_status = 'failed'" in flat:
                (pid,) = params
                self._by_id[pid]["auth_status"] = "failed"
            elif "applied_at" in flat:
                (pid,) = params
                self._by_id[pid]["applied_at"] = "2026-08-24T00:00:00Z"
            return []

        return []

    def transaction(self):  # pragma: no cover -- charge() does not use it
        raise AssertionError("charge() should not open a transaction here")

    def signals(self):
        return [p[0] for p in self.review_inserts]


class _Servicing:
    def __init__(self):
        self.bodies = []

    def post(self, url, json=None, **kwargs):
        self.bodies.append((url, json))
        return _Ok()


@pytest.fixture
def db(monkeypatch):
    fake = _Db()
    monkeypatch.setattr(payments, "db", fake)
    monkeypatch.setattr(review_signals, "db", fake)
    return fake


@pytest.fixture
def servicing(monkeypatch):
    stub = _Servicing()
    monkeypatch.setattr(payments.httpx, "post", stub.post)
    return stub


@pytest.fixture(autouse=True)
def _reset_processor():
    processor._stub_authorizations.clear()
    yield
    processor._stub_authorizations.clear()


def _payload(**over):
    body = {"loan_id": 4471, "processor_token": VALID_MOCK_TOKEN, "last4": "1111",
            "brand": "visa", "amount": 250.00, "method": "card",
            "idempotency_key": "idem-review-1", "source_ref": SOURCE_REF}
    body.update(over)
    return body


# --------------------------------------------------------------------------
# The exact-duplicate signal on a repeated idempotency key.
# --------------------------------------------------------------------------

def test_a_repeated_idempotency_key_raises_an_exact_signal(db, servicing):
    client.post("/payments", json=_payload())
    client.post("/payments", json=_payload())

    assert db.signals() == ["exact_idempotency_key"], (
        "a repeated key produced no exact-duplicate signal")


def test_the_repeat_still_replays_and_charges_nothing_extra(db, servicing):
    """The signal is recorded AROUND the existing control, which still decides
    the outcome."""
    first = client.post("/payments", json=_payload()).json()
    second = client.post("/payments", json=_payload()).json()

    assert second["payment_id"] == first["payment_id"], "a second payment row exists"
    inserts = [sql for sql, _ in db.calls
               if sql.startswith("INSERT INTO payments")]
    assert len(inserts) == 2, "the second attempt did not go through ON CONFLICT"
    assert len(db._by_id) == 1, "a second payments row was created"
    assert processor._stub_authorizations and len(processor._stub_authorizations) == 1, (
        "the retry authorised the card a second time")


def test_many_retries_produce_one_review_item(db, servicing):
    """The client asked not to flood the queue. The unique constraint does the
    real work in Postgres (`db/tests/test_0045_...`); here the point is that the
    INSERT is always the same deduplicating statement, once per attempt, against
    one payment."""
    for _ in range(5):
        client.post("/payments", json=_payload())

    review_inserts = [sql for sql, _ in db.calls
                      if "reconciliation_review_items" in sql and sql.startswith("INSERT")]
    assert all("ON CONFLICT (payment_id, signal_type) DO NOTHING" in sql
               for sql in review_inserts), (
        "the review INSERT is not deduplicated, so retries would pile up")
    assert {p[1] for p in db.review_inserts} == {1}, (
        "the retries flagged more than one payment")


# --------------------------------------------------------------------------
# The heuristic signal, through the real capture path.
# --------------------------------------------------------------------------

def test_a_second_capture_from_the_same_source_inside_the_window_is_flagged(db, servicing):
    client.post("/payments", json=_payload(idempotency_key="idem-a"))
    client.post("/payments", json=_payload(idempotency_key="idem-b"))

    assert "heuristic_30_minute_candidate" in db.signals()


def test_a_second_capture_from_a_different_source_is_not_flagged(db, servicing):
    client.post("/payments", json=_payload(idempotency_key="idem-a"))
    client.post("/payments", json=_payload(idempotency_key="idem-b",
                                           source_ref="src_mock_other"))

    assert "heuristic_30_minute_candidate" not in db.signals()


def test_a_capture_with_no_source_reference_is_not_flagged(db, servicing):
    """An ACH payment has no tokenizer. Unknown is not a match, so the heuristic
    stays silent rather than falling back to loan + amount + channel."""
    client.post("/payments", json=_payload(idempotency_key="idem-a", source_ref=None))
    client.post("/payments", json=_payload(idempotency_key="idem-b", source_ref=None))

    assert "heuristic_30_minute_candidate" not in db.signals()


def test_a_different_channel_is_not_flagged(db, servicing):
    client.post("/payments", json=_payload(idempotency_key="idem-a", method="ach"))
    client.post("/payments", json=_payload(idempotency_key="idem-b", method="card"))

    assert "heuristic_30_minute_candidate" not in db.signals()


# --------------------------------------------------------------------------
# Signals move no money, and cannot break the money path.
# --------------------------------------------------------------------------

def test_flagging_applies_nothing_extra_to_the_balance(db, servicing):
    client.post("/payments", json=_payload(idempotency_key="idem-a"))
    client.post("/payments", json=_payload(idempotency_key="idem-b"))

    applies = [body for url, body in servicing.bodies if "apply-payment" in url]
    assert len(applies) == 2, (
        "the number of balance applications changed when signals were recorded")
    assert db.signals().count("heuristic_30_minute_candidate") == 1

    # No statement anywhere in the capture path writes a ledger entry or adjusts
    # a balance: those belong to servicing, and a review signal must not reach
    # them.
    for sql, _ in db.calls:
        assert "ledger_entries" not in sql
        assert "UPDATE balances" not in sql


def test_a_review_table_outage_does_not_fail_the_payment(monkeypatch, servicing):
    """The asymmetry that matters: an observation failing must never refuse a
    capture that succeeded."""
    fake = _Db(review_insert_fails=True)
    monkeypatch.setattr(payments, "db", fake)
    monkeypatch.setattr(review_signals, "db", fake)

    first = client.post("/payments", json=_payload(idempotency_key="idem-a"))
    second = client.post("/payments", json=_payload(idempotency_key="idem-a"))

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["payment_id"] == first.json()["payment_id"]
    assert fake.review_inserts == [], "the failing insert somehow recorded a row"


def test_the_review_row_carries_no_amount_or_instrument_data(db, servicing):
    """Privacy: the row names the payments, the loan and a correlation handle.
    The reviewer reads the money and the instrument from the payment itself."""
    client.post("/payments", json=_payload(idempotency_key="idem-a"))
    client.post("/payments", json=_payload(idempotency_key="idem-b"))

    for params in db.review_inserts:
        rendered = " ".join(str(p) for p in params)
        for forbidden in ("250", "1111", "visa", VALID_MOCK_TOKEN, SOURCE_REF):
            assert forbidden not in rendered, (
                f"a review item carries {forbidden!r}")


# --------------------------------------------------------------------------
# The two paths review of PR #79 found unreachable or unscreened.
# --------------------------------------------------------------------------

class _CollidingDb(_Db):
    """A payments table whose `processor_ref` is UNIQUE, like the real one.

    `db/migrations/0041` makes that column unique, so the first version of this
    feature could never record its own `exact_provider_transaction_id` signal:
    the capture UPDATE raised the violation, the request failed *after* the card
    was charged, and the collision went unrecorded (RS-001).
    """

    def query(self, sql, params=None):
        flat = " ".join(sql.split())
        if flat.startswith("UPDATE payments") and "processor_ref = %s" in flat:
            auth_id, captured_at, ref, pid = params
            taken = any(row.get("processor_ref") == ref and row["id"] != pid
                        for row in self._by_id.values())
            if taken:
                self.calls.append((flat, params))
                raise UniqueViolation(
                    'duplicate key value violates unique constraint '
                    '"idx_payments_processor_ref"')
        return super().query(sql, params)


def test_a_colliding_settlement_reference_still_records_the_capture(monkeypatch, servicing):
    """The card was charged. Refusing to write that down is the worst outcome."""
    fake = _CollidingDb()
    monkeypatch.setattr(payments, "db", fake)
    monkeypatch.setattr(review_signals, "db", fake)
    # Both captures get the same settlement reference from the processor, which
    # is the collision this test is about.
    monkeypatch.setattr(processor, "_stub_settlement_reference",
                        lambda *a, **k: "PR-COLLIDE-1")

    first = client.post("/payments", json=_payload(idempotency_key="idem-a"))
    second = client.post("/payments", json=_payload(idempotency_key="idem-b"))

    assert first.status_code == 200
    assert second.status_code == 200, (
        "a colliding settlement reference failed the request after the card was "
        "charged")
    assert fake._by_id[2]["auth_status"] == "captured"


def test_a_colliding_settlement_reference_raises_the_exact_signal(monkeypatch, servicing):
    fake = _CollidingDb()
    monkeypatch.setattr(payments, "db", fake)
    monkeypatch.setattr(review_signals, "db", fake)
    monkeypatch.setattr(processor, "_stub_settlement_reference",
                        lambda *a, **k: "PR-COLLIDE-2")

    client.post("/payments", json=_payload(idempotency_key="idem-a"))
    client.post("/payments", json=_payload(idempotency_key="idem-b"))

    assert "exact_provider_transaction_id" in fake.signals(), (
        "the collision produced no exact-duplicate signal, so the one case that "
        "signal exists for cannot reach it")


def test_a_colliding_reference_is_not_stored_on_the_second_row(monkeypatch, servicing):
    """The reference belongs to the other row, and the index is not negotiable.

    A capture with no reference is reported by reconciliation as an
    `unreferenced_capture` break rather than skipped, which is the honest state
    for this row -- and it is why the fallback is safe.
    """
    fake = _CollidingDb()
    monkeypatch.setattr(payments, "db", fake)
    monkeypatch.setattr(review_signals, "db", fake)
    monkeypatch.setattr(processor, "_stub_settlement_reference",
                        lambda *a, **k: "PR-COLLIDE-3")

    client.post("/payments", json=_payload(idempotency_key="idem-a"))
    client.post("/payments", json=_payload(idempotency_key="idem-b"))

    assert fake._by_id[1]["processor_ref"] == "PR-COLLIDE-3"
    assert fake._by_id[2]["processor_ref"] is None, (
        "the second capture kept a settlement reference that belongs to the "
        "first, which the unique index exists to prevent")


def test_a_recovered_capture_is_screened_too(monkeypatch, servicing):
    """A capture completed on the retry path used to be screened by nothing.

    That path is the pending-authorization recovery -- "the processor took the
    money, we crashed before recording it" -- which is exactly the shape a
    duplicate charge arrives in, so leaving it unscreened left the heuristic
    covering only first attempts (RS-003).
    """
    fake = _Db()
    monkeypatch.setattr(payments, "db", fake)
    monkeypatch.setattr(review_signals, "db", fake)

    # An earlier capture from the same source, so the retry has something to
    # resemble.
    client.post("/payments", json=_payload(idempotency_key="idem-earlier"))

    # A row left pending, as a crash between authorization and persistence leaves
    # it -- then retried under the same key, which is what completes it.
    client.post("/payments", json=_payload(idempotency_key="idem-pending"))
    fake._by_id[2]["auth_status"] = "pending"
    fake._by_id[2]["captured_at"] = None
    fake._by_key["idem-pending"]["auth_status"] = "pending"
    fake.review_inserts.clear()

    client.post("/payments", json=_payload(idempotency_key="idem-pending"))

    assert "heuristic_30_minute_candidate" in fake.signals(), (
        "a capture completed on the recovery path raised no heuristic signal")
