"""One identifier, unchanged, from the charge to the apply.

The client asked at the 2026-08-19 demo to follow ONE payment across services.
They could not, and the reason was never missing records -- `ledger_entries` is
immutable and every movement names its actor. The reason was that each hop is
keyed by something different: the authorization by `idempotency_key`, the row by
its serial `id`, the ledger by `payment_id` -- which does not exist until after
the INSERT, so the leg where money actually leaves had no key at all.

**Auditable is not traceable, and these tests are about the second one.**

The failure this file is built around is not "the id is missing". It is the id
being SILENTLY REPLACED: every service logs something that looks like a trace,
each line is individually correct, and nothing joins. So the assertions compare
the id ACROSS boundaries rather than checking that each side has one, and the
adversarial cases each break a different boundary.

`correlation_id` is deliberately NOT the idempotency key:

  * `idempotency_key` is caller-supplied and DECIDES something -- whether two
    requests are the same payment. Widening its job would let a caller choose
    how our own evidence is indexed.
  * `correlation_id` is server-minted and decides nothing. Replace every value
    tomorrow and no balance moves.

Synthetic data only.
"""
import logging
import re
import uuid
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import config, payments, processor, reconcile
from app.main import app

client = TestClient(app)
client.headers.update({"X-Internal-Token": config.INTERNAL_SERVICE_TOKEN})

VALID_MOCK_TOKEN = "tok_mock_550e8400-e29b-41d4-a716-446655440000"
CORRELATION_RE = re.compile(r"^pay_[0-9a-f]{32}$")


class _Ok:
    status_code = 200

    def raise_for_status(self):
        pass


class _Db:
    """A payments table that records statements and honours the unique key."""

    def __init__(self):
        self.calls = []
        self._next_id = 1
        self._by_key = {}
        self._by_id = {}

    def query(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params))
        stmt = sql.strip()
        if stmt.startswith("INSERT"):
            (loan_id, last4, brand, amount, method, key, correlation_id,
             source_ref) = params
            if key is not None and key in self._by_key:
                return []
            row = {"id": self._next_id, "loan_id": loan_id,
                   "amount": Decimal(str(amount)), "last4": last4, "brand": brand,
                   "applied_at": None, "auth_status": "pending",
                   "correlation_id": correlation_id}
            self._next_id += 1
            if key is not None:
                self._by_key[key] = row
            self._by_id[row["id"]] = row
            return [row]
        if stmt.startswith("SELECT"):
            (key,) = params
            return [self._by_key[key]]
        if stmt.startswith("UPDATE"):
            if "auth_status = 'captured'" in stmt:
                auth_id, _captured_at, _ref, pid = params
                self._by_id[pid].update(auth_status="captured", authorization_id=auth_id)
            elif "auth_status = 'failed'" in stmt:
                (pid,) = params
                self._by_id[pid]["auth_status"] = "failed"
            elif "applied_at" in stmt:
                (pid,) = params
                self._by_id[pid]["applied_at"] = "2026-08-20T00:00:00Z"
            elif "apply_last_error" in stmt:
                pass
            else:
                raise AssertionError("unexpected UPDATE: " + sql)
            return []
        raise AssertionError("unexpected query: " + sql)

    def stored_correlation_id(self, payment_id=1):
        return self._by_id[payment_id]["correlation_id"]


class _Servicing:
    """Captures what payment-service actually put on the wire to servicing."""

    def __init__(self):
        self.bodies = []

    def post(self, url, json=None, **kwargs):
        self.bodies.append(json)
        return _Ok()


@pytest.fixture
def db(monkeypatch):
    fake = _Db()
    monkeypatch.setattr(payments, "db", fake)
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
    body = {"loan_id": 42, "processor_token": VALID_MOCK_TOKEN, "last4": "1111",
            "brand": "visa", "amount": 250.0, "idempotency_key": "idem-corr-1"}
    body.update(over)
    return body


# --------------------------------------------------------------------------
# The identifier itself.
# --------------------------------------------------------------------------

def test_a_capture_mints_one_opaque_identifier(db, servicing):
    client.post("/payments", json=_payload())

    stored = db.stored_correlation_id()
    assert CORRELATION_RE.match(stored), stored


def test_the_identifier_carries_no_business_or_card_data(db, servicing, monkeypatch):
    """A correlator embedding the loan or the amount would leak context into
    every log line quoting it, and would stop being opaque the moment a reader
    learned the format.

    Asserted by pinning the only entropy the minter has and demanding the whole
    identifier equal it. Two reasons, and the second is the interesting one:

    * the previous version searched the stored id for "4471", "250", "1111" and
      the token, and a random `uuid4().hex` contains "250" roughly once in 140
      runs -- so it failed on CI about once in a hundred runs for a reason that
      had nothing to do with a leak. A control that cries wolf is how people
      learn to re-run the suite instead of reading it;
    * substring absence is also weaker than it looks. It passes a minter that
      hashes the loan id into the identifier, or reverses its digits, or encodes
      it in hex -- all of which leak exactly what this test exists to prevent.
      Equality against the pinned entropy fails for any input that reaches the
      identifier by any route.

    The patch is scoped to the `payments` module's own `uuid` name rather than
    the global module, so nothing else in the request path is affected by it.
    """
    fixed = uuid.UUID("0123456789abcdef0123456789abcdef")
    monkeypatch.setattr(payments, "uuid", SimpleNamespace(uuid4=lambda: fixed))

    client.post("/payments", json=_payload(loan_id=4471, amount=250.0))

    assert db.stored_correlation_id() == f"pay_{fixed.hex}", (
        "the correlation id is not purely the entropy the minter was given, so "
        "something from the request reached it")


def test_two_captures_do_not_share_an_identifier(db, servicing):
    """The complement to the test above, unpatched: pinning the entropy proves
    nothing about uniqueness, and an id that is opaque but constant correlates
    every payment with every other one."""
    client.post("/payments", json=_payload(idempotency_key="idem-corr-uniq-1"))
    first = db.stored_correlation_id()

    client.post("/payments", json=_payload(idempotency_key="idem-corr-uniq-2"))
    second = db.stored_correlation_id(payment_id=2)

    assert CORRELATION_RE.match(first) and CORRELATION_RE.match(second)
    assert first != second, "two captures share one correlation id"


def test_it_is_not_the_idempotency_key(db, servicing):
    """The two must not converge. The key is caller-supplied and decides dedupe;
    this decides nothing. If someone later reuses one for the other, this
    fails."""
    client.post("/payments", json=_payload(idempotency_key="idem-corr-1"))

    assert db.stored_correlation_id() != "idem-corr-1"


# --------------------------------------------------------------------------
# The boundaries -- each a place the id could be dropped or replaced.
# --------------------------------------------------------------------------

def test_the_same_identifier_reaches_servicing(db, servicing):
    """What actually went on the wire, not what was passed around in process."""
    client.post("/payments", json=_payload())

    assert servicing.bodies, "no apply call was made"
    assert servicing.bodies[0]["correlation_id"] == db.stored_correlation_id()


def test_the_same_identifier_appears_in_this_service_s_logs(db, servicing, caplog):
    """An id nobody can grep is not a trace. Asserted on captured output,
    because logging is where an operator meets it."""
    caplog.set_level(logging.INFO)
    client.post("/payments", json=_payload())

    stored = db.stored_correlation_id()
    lines = [r.getMessage() for r in caplog.records if stored in r.getMessage()]
    assert len(lines) >= 2, (
        "the correlation id appears in %d log line(s); the charge and the apply "
        "should both carry it" % len(lines)
    )


def test_the_processor_call_carries_it_as_a_header(monkeypatch):
    """The leg no other identifier can cover: this call happens before the
    payments row has an id, so anything keyed on `payment_id` starts a hop
    late."""
    sent = []

    class _Approved:
        def raise_for_status(self):
            pass

        def json(self):
            return {"approved": True, "authorization_id": "auth_1",
                    "processor_ref": "PR-1"}

    monkeypatch.setattr(processor, "PROCESSOR_API_KEY", "test-key")
    monkeypatch.setattr(processor.httpx, "post",
                        lambda *a, **k: sent.append(k) or _Approved())

    processor.authorize_charge(VALID_MOCK_TOKEN, 10.0, "idem-x",
                               correlation_id="pay_deadbeef")

    assert sent[0]["headers"]["X-Correlation-Id"] == "pay_deadbeef"


# --------------------------------------------------------------------------
# Adversarial: the ways one payment ends up with two traces.
# --------------------------------------------------------------------------

def test_a_retry_reuses_the_original_identifier_rather_than_minting_a_second(db, monkeypatch):
    """The case an incident actually exercises, and the reason it is set up the
    hard way.

    A retry is the SAME payment, so it belongs to the same trace. A second id
    would split one payment's evidence in two while every individual log line
    still looked correct.

    The first attempt must FAIL its apply, and that is not incidental. A retry
    after a successful apply never calls servicing again, so the id it would
    have sent is never observable -- an earlier version of this test set it up
    that way, and re-minting the id on the retry path did not fail it. The
    captured-but-unapplied case is both the realistic incident and the only
    shape that can actually catch the defect.
    """
    class _Down(Exception):
        pass

    def _boom(*a, **k):
        raise _Down("servicing is unreachable")

    monkeypatch.setattr(payments.httpx, "post", _boom)
    first = client.post("/payments", json=_payload())
    assert first.status_code == 200
    assert first.json()["status"] == "pending", first.text
    original = db.stored_correlation_id()

    sent = []
    monkeypatch.setattr(payments.httpx, "post",
                        lambda url, json=None, **k: sent.append(json) or _Ok())
    second = client.post("/payments", json=_payload())
    assert second.status_code == 200

    assert db.stored_correlation_id() == original, "the stored id changed on retry"
    assert sent, "the retry did not re-attempt the apply, so nothing was proved"
    assert sent[0]["correlation_id"] == original, (
        "the retry sent servicing %r where the original payment is %r -- one "
        "payment, two traces" % (sent[0]["correlation_id"], original)
    )


def test_the_reconciler_drain_carries_the_row_s_own_identifier(monkeypatch):
    """The drain runs long after the capture, which makes it the likeliest place
    for a trace to be dropped or re-minted -- and the one nobody watches."""
    sent = []
    monkeypatch.setattr(payments.httpx, "post",
                        lambda url, json=None, **k: sent.append(json) or _Ok())
    monkeypatch.setattr(payments, "db", _Db())
    monkeypatch.setattr(reconcile, "claim_due", lambda limit=20: [
        {"id": 7, "loan_id": 42, "amount": Decimal("10.00"),
         "apply_attempts": 1, "correlation_id": "pay_fromtherow"}
    ])

    reconcile.reconcile_once()

    assert sent, "the drain made no apply call"
    assert sent[0]["correlation_id"] == "pay_fromtherow", (
        "the drain replaced or dropped the payment's own correlation id"
    )


def test_a_caller_cannot_choose_the_identifier(db):
    """Server-minted, and enforced rather than intended.

    A caller-chosen correlator lets someone collide two unrelated payments into
    one trace, or push content of their choosing into a column read back into
    log lines. `PaymentIn`'s `extra="forbid"` refuses it.
    """
    resp = client.post("/payments", json=_payload(correlation_id="pay_attacker"))

    assert resp.status_code == 422, resp.text
    assert db.calls == [], "a refused request still wrote to the payments table"


def test_two_separate_payments_get_two_identifiers(db, servicing):
    """Guard the guard. A constant would satisfy every same-id assertion above
    while making the column useless -- everything would correlate with
    everything."""
    client.post("/payments", json=_payload(idempotency_key="idem-a"))
    first = db.stored_correlation_id(1)
    client.post("/payments", json=_payload(idempotency_key="idem-b"))
    second = db.stored_correlation_id(2)

    assert first != second


def test_no_log_line_carries_an_id_that_matches_no_row(db, monkeypatch, caplog):
    """Reviewed finding CORR-LOG-001, and the failure it describes exactly.

    The id has to be minted before the INSERT, because the processor call needs
    it and `payment_id` does not exist yet. On a RETRY that pre-mint is thrown
    away in favour of the stored one -- so any line logged before the row is
    resolved is indexed under an id that matches no `payments` row and no
    `ledger_entries` row.

    An operator grepping that id finds nothing, on the one path this PR calls
    the realistic incident. A dead id in a trace is worse than no id: it looks
    like evidence and returns an empty result.

    So the assertion is not "the canonical id appears somewhere" -- it is that
    NO `pay_` value appears anywhere in the retry's output except the canonical
    one.
    """
    import logging
    import re as _re

    def _boom(*a, **k):
        raise RuntimeError("servicing is unreachable")

    monkeypatch.setattr(payments.httpx, "post", _boom)
    assert client.post("/payments", json=_payload()).status_code == 200
    canonical = db.stored_correlation_id()

    monkeypatch.setattr(payments.httpx, "post",
                        lambda url, json=None, **k: _Ok())
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        assert client.post("/payments", json=_payload()).status_code == 200

    logged = chr(10).join(r.getMessage() for r in caplog.records)
    seen = set(_re.findall(r"pay_[0-9a-f]{32}", logged))
    assert seen <= {canonical}, (
        "the retry logged correlation id(s) that belong to no payment row: %r "
        "(canonical is %r)" % (sorted(seen - {canonical}), canonical)
    )
    assert canonical in logged, "the retry logged no canonical id at all"


def test_the_duplicate_path_lines_can_be_found_by_the_trace(db, monkeypatch, caplog):
    """The lines an operator reads during an incident must carry the id.

    A retry writes its own explanatory lines -- 'still pending authorization',
    'not yet applied, retrying apply'. Those are precisely what someone follows
    when a payment is stuck, and a line without the id cannot be reached from
    the trace at all.
    """
    import logging

    def _boom(*a, **k):
        raise RuntimeError("servicing is unreachable")

    monkeypatch.setattr(payments.httpx, "post", _boom)
    client.post("/payments", json=_payload())
    canonical = db.stored_correlation_id()

    monkeypatch.setattr(payments.httpx, "post", lambda url, json=None, **k: _Ok())
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        client.post("/payments", json=_payload())

    retry_lines = [r.getMessage() for r in caplog.records
                   if "duplicate POST /payments" in r.getMessage()]
    assert retry_lines, "the retry logged no duplicate-path line"
    for line in retry_lines:
        assert canonical in line, (
            "a duplicate-path line cannot be found by the trace: %r" % line
        )


def test_a_retry_on_a_pre_trace_row_propagates_no_id_at_all(db, monkeypatch, caplog):
    """Reviewed finding CORR-NULL-001: the dead id returning through history.

    A payment captured before this column existed has `correlation_id` NULL, and
    the migration is explicit that NULL means "written before the trace existed"
    -- a true statement rather than a gap. The duplicate path used to fall back
    to the fresh pre-insert mint for such a row, and nothing ever persists that
    value, so a retry would log an id, send it to the processor and stamp new
    ledger entries with it while the `payments` row stayed NULL. Unfindable by
    the very id it was advertising.

    NULL stays NULL. The alternative -- back-filling the row on retry -- was
    rejected deliberately: the capture and its authorization already happened
    without an id, so the resulting trace would cover the tail of the payment
    while looking complete, which is the same "looks like evidence, returns
    nothing" failure in a subtler form.
    """
    import logging
    import re as _re

    def _boom(*a, **k):
        raise RuntimeError("servicing is unreachable")

    monkeypatch.setattr(payments.httpx, "post", _boom)
    assert client.post("/payments", json=_payload()).status_code == 200

    # The row as a pre-0043 capture looks: applied_at NULL, no trace.
    db._by_key["idem-corr-1"]["correlation_id"] = None

    sent = []
    monkeypatch.setattr(payments.httpx, "post",
                        lambda url, json=None, **k: sent.append(json) or _Ok())
    caplog.clear()
    with caplog.at_level(logging.DEBUG):
        assert client.post("/payments", json=_payload()).status_code == 200

    assert sent, "the retry did not re-attempt the apply"
    assert sent[0]["correlation_id"] is None, (
        "the retry sent servicing %r for a payment whose row has no id"
        % (sent[0]["correlation_id"],)
    )

    logged = chr(10).join(r.getMessage() for r in caplog.records)
    assert not _re.findall(r"pay_[0-9a-f]{32}", logged), (
        "the retry logged an id that is not on the payments row: %r"
        % _re.findall(r"pay_[0-9a-f]{32}", logged)
    )
    assert db.stored_correlation_id() is None, (
        "the retry back-filled the row instead of leaving the pre-trace payment "
        "without a trace"
    )
