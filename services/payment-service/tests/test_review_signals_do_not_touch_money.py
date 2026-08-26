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
import re
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


#: The positional shape of the review INSERT, from
#: `review_signals._record_review_item`. Named here because the assertion below
#: needs to treat one column differently from the rest, and doing that by index
#: silently rots the day a column is added.
REVIEW_COLUMNS = ("signal_type", "payment_id", "related_payment_id", "loan_id",
                  "correlation_ref", "queue")

#: Values that would mean money or an instrument had reached the review row.
#: `250` is the amount and `1111` the card's last four, and both are just DIGITS
#: -- which matters for where they can be searched for.
FORBIDDEN_VALUES = ("250", "1111", "visa", VALID_MOCK_TOKEN, SOURCE_REF)


def test_the_review_row_carries_no_amount_or_instrument_data(db, servicing):
    """Privacy: the row names the payments, the loan and a correlation handle.
    The reviewer reads the money and the instrument from the payment itself.

    **Why this is not a substring scan over the whole row.** It was, and that
    made it flaky at about one run in a hundred. `correlation_ref` is a
    server-minted uuid4, and a uuid4 contains the digits "250" roughly 0.5% of
    the time (measured: 0.0056 over 200k samples, and this test mints two of
    them). So the assertion failed on a random identifier that had nothing to do
    with the amount -- a red build reporting a privacy leak that did not exist,
    which is worse than no test, because the next person to see it learns to
    re-run rather than to read it.

    The digit sentinels are therefore checked against the columns where they
    would actually mean something, and `correlation_ref` is asserted for what it
    IS: an opaque handle that carries no content of its own. Nothing about the
    guarantee is weakened -- an amount reaching the row would land in a column
    this still scans, and an amount smuggled INTO the correlation ref is caught
    by the shape assertion below.
    """
    client.post("/payments", json=_payload(idempotency_key="idem-a"))
    client.post("/payments", json=_payload(idempotency_key="idem-b"))

    assert db.review_inserts, "no review item was recorded, so this proves nothing"
    assert_review_rows_carry_no_money(db.review_inserts)


#: `payments.new_correlation_id()` returns `pay_` + a uuid4 hex. Anchored, so a
#: value with anything appended or prepended fails.
_OPAQUE_HANDLE = re.compile(r"^pay_[0-9a-f]{32}$")


def assert_review_rows_carry_no_money(review_inserts) -> None:
    """The row-privacy assertion, in one place so it can be tested itself.

    Review finding RVG-001: the guard-the-guard test below used to assert on its
    OWN filtered list rather than on this assertion, which meant it would have
    stayed green if someone deleted the opaque-handle check from the real test.
    A guard that re-implements what it guards is not guarding it. Now both the
    real test and the guard call this, and the guard calls it under
    `pytest.raises`.
    """
    correlation_index = REVIEW_COLUMNS.index("correlation_ref")
    for params in review_inserts:
        assert len(params) == len(REVIEW_COLUMNS), (
            "the review INSERT changed shape; update REVIEW_COLUMNS so this test "
            "keeps scanning the columns it thinks it is scanning")

        content = " ".join(
            str(p) for i, p in enumerate(params) if i != correlation_index)
        for forbidden in FORBIDDEN_VALUES:
            assert forbidden not in content, (
                f"a review item carries {forbidden!r}")

        # The correlation ref is opaque, and that is the whole of its contract:
        # server-minted, decides nothing, derived from nothing about the payment.
        # Asserted by SHAPE rather than by substring, so a random uuid cannot
        # fail it and a handcrafted "corr-250.00-visa" cannot pass it.
        ref = params[correlation_index]
        assert ref is None or _is_opaque_handle(ref), (
            f"the correlation ref {ref!r} is not an opaque server-minted handle")
        if ref is not None:
            for forbidden in ("visa", VALID_MOCK_TOKEN, SOURCE_REF):
                assert forbidden not in str(ref), (
                    f"the correlation ref carries {forbidden!r}")


def test_a_correlation_ref_that_happens_to_contain_the_amount_is_not_a_leak(
        db, servicing, monkeypatch):
    """The flake this file used to have, pinned so it cannot come back.

    A uuid4 contains the digits "250" about 0.5% of the time -- measured at
    0.0056 over 200k samples -- and this path mints one per payment. The previous
    assertion rendered every INSERT parameter into one string and searched it for
    "250", so roughly one CI run in a hundred reported a privacy leak that did
    not exist.

    Here the collision is forced rather than waited for: the correlation id is
    pinned to a value containing both digit sentinels. The row is still clean,
    because those digits are in an opaque server-minted handle and not in any
    column that carries money.
    """
    monkeypatch.setattr(payments, "new_correlation_id",
                        lambda: "pay_250abc1111def4444abcd5555ef66660")

    client.post("/payments", json=_payload(idempotency_key="idem-a"))
    client.post("/payments", json=_payload(idempotency_key="idem-b"))

    assert db.review_inserts, "no review item was recorded, so this proves nothing"
    correlation_index = REVIEW_COLUMNS.index("correlation_ref")
    for params in db.review_inserts:
        ref = params[correlation_index]
        # The digits really are present in the handle -- otherwise this test is
        # not exercising the collision it claims to.
        assert ref is None or "250" in str(ref)
        content = " ".join(
            str(p) for i, p in enumerate(params) if i != correlation_index)
        for forbidden in FORBIDDEN_VALUES:
            assert forbidden not in content


def test_an_amount_smuggled_into_the_correlation_ref_is_still_caught(
        db, servicing, monkeypatch):
    """Guard the guard: excluding a column from the scan must not blind it.

    The fix above stops scanning `correlation_ref` for digits. That would be a
    hole if the shape check were weak, so this pins a ref that is NOT an opaque
    handle -- it carries the amount and the brand in readable form -- and
    requires the assertion to reject it.
    """
    monkeypatch.setattr(payments, "new_correlation_id",
                        lambda: "corr-250.00-visa-1111")

    client.post("/payments", json=_payload(idempotency_key="idem-a"))
    client.post("/payments", json=_payload(idempotency_key="idem-b"))

    assert db.review_inserts

    # Calls the REAL assertion, not a re-implementation of it. Review finding
    # RVG-001: the previous version filtered the rows itself and asserted its own
    # list was non-empty, so deleting the opaque-handle check from
    # `assert_review_rows_carry_no_money` would have left this green. Now the
    # thing under test is the assertion.
    with pytest.raises(AssertionError, match="opaque server-minted handle"):
        assert_review_rows_carry_no_money(db.review_inserts)


def _is_opaque_handle(value) -> bool:
    """True when `value` is exactly a server-minted correlation handle.

    Deliberately strict, and matched against the real generator's format rather
    than a guess: the first version of this helper accepted only a BARE uuid and
    failed on the actual `pay_<hex>` value, which is the sort of thing a test
    should discover about itself before a reviewer does.

    A `pay_` prefix plus 32 hex characters cannot encode an amount, a card brand
    or a token, so proving the shape proves the absence of content -- which a
    substring scan over a random identifier can never do in either direction.
    """
    return bool(_OPAQUE_HANDLE.match(str(value)))


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
