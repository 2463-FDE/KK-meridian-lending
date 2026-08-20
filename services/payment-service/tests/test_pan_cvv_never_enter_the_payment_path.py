"""A synthetic capture, then: no card number and no security code anywhere.

The client asked at the 2026-08-19 demo what happens to the CVV and the full
PAN in the payment path. "The columns were dropped" answers a narrower question
than the one asked: a value can be absent from storage and still pass through a
process, a SQL parameter, a log line or an in-process cache.

The existing tests each cover one channel by name --
`test_charge_flow.py::test_post_payment_rejects_pan_cvv_ssn_outright` covers the
fields *called* pan/cvv/ssn, `test_redactor.py` covers the patterns, and
`test_cardholder_name_not_logged.py` covers one field. What none of them does is
run a whole capture with real card-shaped values and then sweep EVERYTHING the
capture touched. That is the difference between "the field named `pan` is
refused" and "the value cannot get in".

So this file runs a synthetic capture and asserts over two complete surfaces:

  1. every SQL statement and every bound parameter `charge()` produced, and
  2. every log record emitted while it ran,

for a Luhn-valid test PAN and a CVV, pushed through each caller-controlled
field in turn. A field that refuses the value at the boundary satisfies the
claim; so does one that accepts it and never lets it reach either surface. What
fails is a value that arrives somewhere.

The sweep is guarded against passing vacuously: `test_the_sweep_would_catch_a_
planted_value` plants the PAN in both surfaces and asserts the helpers report
it. A negative assertion over a surface that is empty -- or over a helper that
looks in the wrong place -- passes against a system that leaks everything, and
that class of false pass is the one this repository has already been bitten by.

**Synthetic data only.** `4111111111111111` is the published Visa test number
and `123` is not anybody's security code. Nothing here is a real card.

Written up in `docs/PAN-CVV-DATA-FLOW.md`, which also names the boundaries this
CANNOT prove -- the mock tokenizer, logs outside the application, caches outside
our control, and pre-migration backups.
"""
import logging
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import config, payments, processor, schemas
from app.main import app

client = TestClient(app)
client.headers.update({"X-Internal-Token": config.INTERNAL_SERVICE_TOKEN})

# The published Visa test number. Luhn-valid on purpose: the redactor only masks
# a digit run that passes Luhn, so a made-up number would prove nothing about
# the guard that matters.
SYNTHETIC_PAN = "4111111111111111"
SYNTHETIC_CVV = "123"
# Every contiguous fragment that would still be card data if it escaped. The
# last four ARE allowed to appear -- they are `last4`, the whole point -- so the
# fragments checked are the ones that must not.
PAN_FRAGMENTS = (SYNTHETIC_PAN, SYNTHETIC_PAN[:12], SYNTHETIC_PAN[:6])

# Shaped like frontend/lib/tokenize.ts's own output; the stub processor only
# approves this shape, exactly as a real processor only approves a token it
# issued.
VALID_MOCK_TOKEN = "tok_mock_550e8400-e29b-41d4-a716-446655440000"


class _RecordingDb:
    """A payments table that remembers every statement and parameter.

    Only as much behaviour as `charge()` needs -- an insert that returns a row,
    a read-back by key, and the capture/apply UPDATEs -- because the assertions
    are about what was PASSED, not about what a database would do with it.
    """

    def __init__(self):
        self.calls = []
        self._next_id = 1
        self._by_key = {}
        self._by_id = {}

    def query(self, sql, params=None):
        self.calls.append((sql, params))
        stmt = sql.strip()
        if stmt.startswith("INSERT"):
            loan_id, last4, brand, amount, method, idempotency_key = params
            if idempotency_key is not None and idempotency_key in self._by_key:
                return []
            row = {
                "id": self._next_id, "loan_id": loan_id,
                "amount": Decimal(str(amount)), "last4": last4, "brand": brand,
                "applied_at": None, "auth_status": "pending",
                "authorization_id": None,
            }
            self._next_id += 1
            if idempotency_key is not None:
                self._by_key[idempotency_key] = row
            self._by_id[row["id"]] = row
            return [row]
        if stmt.startswith("SELECT"):
            (idempotency_key,) = params
            return [self._by_key[idempotency_key]]
        if stmt.startswith("UPDATE"):
            if "auth_status = 'captured'" in stmt:
                auth_id, captured_at, processor_ref, payment_id = params
                self._by_id[payment_id].update(
                    auth_status="captured", authorization_id=auth_id,
                    captured_at=captured_at, processor_ref=processor_ref)
            elif "auth_status = 'failed'" in stmt:
                (payment_id,) = params
                self._by_id[payment_id]["auth_status"] = "failed"
            elif "applied_at" in stmt:
                (payment_id,) = params
                self._by_id[payment_id]["applied_at"] = "2026-08-20T00:00:00Z"
            elif "apply_last_error" in stmt:
                error_code, payment_id = params
                self._by_id[payment_id]["apply_last_error"] = error_code
            else:
                raise AssertionError(f"unexpected UPDATE: {sql}")
            return []
        raise AssertionError(f"unexpected query: {sql}")

    # --- the surface under test -------------------------------------------
    def written(self) -> str:
        """Every statement and every bound parameter, as one searchable string.

        Statements included as well as parameters: a value interpolated into SQL
        text instead of bound would be the same leak with a worse bug attached.
        """
        parts = []
        for sql, params in self.calls:
            parts.append(sql)
            for value in (params or ()):
                parts.append(repr(value))
        return "\n".join(parts)


class _FakeServicingResponse:
    status_code = 200

    def raise_for_status(self):
        pass


@pytest.fixture
def recording_db(monkeypatch):
    db = _RecordingDb()
    monkeypatch.setattr(payments, "db", db)
    return db


@pytest.fixture(autouse=True)
def _stub_servicing_call(monkeypatch):
    monkeypatch.setattr(payments.httpx, "post",
                        lambda *a, **k: _FakeServicingResponse())


@pytest.fixture(autouse=True)
def _reset_stub_processor_authorizations():
    processor._stub_authorizations.clear()
    yield
    processor._stub_authorizations.clear()


@pytest.fixture
def logged(caplog):
    caplog.set_level(logging.DEBUG)
    return caplog


def _logged_text(caplog) -> str:
    return "\n".join(r.getMessage() for r in caplog.records)


def _payload(**overrides):
    body = {
        "loan_id": 42, "processor_token": VALID_MOCK_TOKEN, "last4": "1111",
        "brand": "visa", "amount": 250.0, "idempotency_key": "idem-cardpath-1",
    }
    body.update(overrides)
    return body


def _assert_no_card_data(surface: str, where: str):
    for fragment in PAN_FRAGMENTS:
        assert fragment not in surface, (
            f"the card number reached {where} -- found {fragment!r}")
    # The CVV is only three digits, so a bare "123" is not evidence of anything;
    # what must not appear is a security code IDENTIFIED as one, which is the
    # shape the redactor exists to catch and the shape a leak actually takes.
    lowered = surface.lower()
    for phrasing in ("cvv", "cvc", "security code", "card verification"):
        if phrasing in lowered:
            index = lowered.index(phrasing)
            window = surface[index:index + 60]
            assert SYNTHETIC_CVV not in window, (
                f"a security code reached {where}: {window!r}")


# --------------------------------------------------------------------------
# Guard the guard.
# --------------------------------------------------------------------------

def test_the_sweep_would_catch_a_planted_value():
    """The negative assertions must be able to fail.

    Both helpers are pointed at a surface that genuinely contains the values. If
    this test ever passes trivially, every assertion below it is decoration.
    """
    with pytest.raises(AssertionError):
        _assert_no_card_data(f"INSERT ... {SYNTHETIC_PAN}", "a planted statement")
    with pytest.raises(AssertionError):
        _assert_no_card_data(f"charge req cvv={SYNTHETIC_CVV}", "a planted log line")


def test_the_recording_db_actually_records(recording_db):
    """And the surface is non-empty for a real capture, or the sweep is vacuous."""
    resp = client.post("/payments", json=_payload())
    assert resp.status_code == 200
    assert recording_db.calls, "no statement was recorded, so nothing was swept"
    assert "INSERT INTO payments" in recording_db.written()


# --------------------------------------------------------------------------
# The capture itself.
# --------------------------------------------------------------------------

def test_a_synthetic_capture_writes_no_card_data_to_the_database(recording_db):
    """The demo step, as an assertion: one capture, then look at the rows."""
    resp = client.post("/payments", json=_payload())

    assert resp.status_code == 200
    assert resp.json()["status"] == "captured"
    _assert_no_card_data(recording_db.written(), "a SQL statement or parameter")


def test_a_synthetic_capture_writes_no_card_data_to_the_log(recording_db, logged):
    resp = client.post("/payments", json=_payload())

    assert resp.status_code == 200
    _assert_no_card_data(_logged_text(logged), "a log record")


def test_the_capture_still_records_what_it_is_for(recording_db, logged):
    """A path that logged and stored nothing would pass every test above.

    The claim is that card data is absent, not that the payment path is inert.
    """
    client.post("/payments", json=_payload())

    written = recording_db.written()
    assert "'1111'" in written, "last4 is what makes a payment identifiable"
    assert "'visa'" in written
    assert "1111" in _logged_text(logged)


def test_the_insert_names_only_non_card_columns(recording_db):
    """The column list itself, not just this run's values.

    A future column called `card_number` would carry NULL on this synthetic
    capture and slip past a value sweep entirely.
    """
    client.post("/payments", json=_payload())

    inserts = [sql for sql, _ in recording_db.calls if sql.strip().startswith("INSERT")]
    assert len(inserts) == 1
    columns = inserts[0].split("(", 1)[1].split(")", 1)[0]
    assert set(c.strip() for c in columns.split(",")) == {
        "loan_id", "last4", "brand", "amount", "method", "idempotency_key",
        "auth_status",
    }


def test_the_processor_token_is_used_and_never_stored(recording_db):
    """A vaulted token is sensitive even though it is opaque (ADR 0008).

    Asserted here as well as in `test_charge_flow.py` because the data-flow
    statement claims it in step 4, and a claim in a document with its evidence
    in another file's unrelated test is how citations go stale.
    """
    client.post("/payments", json=_payload())

    assert VALID_MOCK_TOKEN not in recording_db.written()


# --------------------------------------------------------------------------
# Every caller-controlled channel, one at a time.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("field", ["processor_token", "brand", "method", "name",
                                   "idempotency_key", "last4"])
def test_a_pan_pushed_through_an_allowed_field_reaches_neither_surface(
        recording_db, logged, field):
    """The fields named pan/cvv/ssn are refused outright, and that is tested
    elsewhere. This is the harder case: the caller uses a field that IS allowed
    as the carrier.

    Two acceptable outcomes, and the test does not care which: the boundary
    refuses the request, or it accepts it and the value still reaches no
    statement, no parameter and no log line. What is not acceptable is the value
    arriving anywhere.
    """
    resp = client.post("/payments", json=_payload(**{field: SYNTHETIC_PAN}))

    assert resp.status_code in (200, 422), resp.text
    _assert_no_card_data(recording_db.written(), f"a statement, via {field}")
    _assert_no_card_data(_logged_text(logged), f"a log record, via {field}")


def test_a_pan_shaped_loan_id_is_refused_by_the_boundary(recording_db):
    """`loan_id` is bounded to int4 for this reason (PR #16).

    An unbounded integer field is a channel for a card number that reaches the
    log line before PostgreSQL ever sees the value.
    """
    resp = client.post("/payments", json=_payload(loan_id=int(SYNTHETIC_PAN)))

    assert resp.status_code == 422
    assert recording_db.calls == [], "a refused request must write nothing"


def test_a_pan_in_the_processor_token_never_becomes_a_captured_row(recording_db):
    """The one free-form string field, followed all the way to what it writes.

    `processor._stub_authorize` builds `authorization_id` from the token's last
    twelve characters, and that id IS persisted -- so a token that reached
    authorization would put twelve digits of a card number on the payments row.
    It does not, because a token that is not a recognised token is declined
    before anything is captured. Asserted on the outcome rather than on the
    regex, because the regex is what would change.
    """
    resp = client.post("/payments", json=_payload(processor_token=SYNTHETIC_PAN))

    assert resp.status_code in (200, 422)
    if resp.status_code == 200:
        assert resp.json()["status"] == "failed", (
            "an arbitrary token was treated as an authorization")
    written = recording_db.written()
    assert "auth_status = 'captured'" not in written
    _assert_no_card_data(written, "a statement, via the processor token")


@pytest.mark.parametrize("phrasing", [
    "cvv {cvv}", "cvc: {cvv}", "security code {cvv}", "card verification value {cvv}",
])
def test_a_security_code_pushed_through_a_free_text_field_reaches_neither_surface(
        recording_db, logged, phrasing):
    """The CVV, in the shapes a caller would actually spell it.

    `idempotency_key` is the free-text field that gets persisted, so it is the
    one that matters; the boundary validator rejects a key carrying any shape the
    redactor knows, which is why this is a 422 rather than a masked write.
    """
    value = phrasing.format(cvv=SYNTHETIC_CVV)
    resp = client.post("/payments", json=_payload(idempotency_key=value))

    assert resp.status_code in (200, 422), resp.text
    _assert_no_card_data(recording_db.written(), "a statement, via idempotency_key")
    _assert_no_card_data(_logged_text(logged), "a log record, via idempotency_key")


# --------------------------------------------------------------------------
# The read model, and the only process-lifetime state there is.
# --------------------------------------------------------------------------

def test_the_payment_read_model_can_only_expose_non_card_fields():
    """What a caller can ever be handed back, from the schema rather than a row.

    A response model is a data-flow boundary too: adding a `pan` field here
    would expose card data on a read path without touching any write path, so
    none of the write-side tests above would notice.
    """
    assert set(schemas.PaymentItem.model_fields) == {
        "id", "amount", "method", "last4", "brand", "created_at",
    }


def test_the_only_in_process_store_holds_no_card_data(recording_db):
    """§2 step 8 of the data-flow statement, executed.

    There is no application cache in this service. The one piece of state that
    outlives a request is the MOCK processor's own idempotency-key store, and
    what it retains is an `Authorization` -- an id, a timestamp and a settlement
    reference. Asserted rather than asserted-about, because "we have no cache"
    is exactly the kind of claim that quietly stops being true.
    """
    client.post("/payments", json=_payload())

    assert processor._stub_authorizations, "the capture recorded no authorization"
    for auth in processor._stub_authorizations.values():
        assert set(auth._fields) == {"authorization_id", "captured_at", "processor_ref"}
        _assert_no_card_data(repr(auth), "the processor's own key store")
    _assert_no_card_data(repr(processor._stub_authorizations),
                         "the processor's own key store")
