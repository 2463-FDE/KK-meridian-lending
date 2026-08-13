"""A capture recovered after a crash keeps the PROCESSOR's timestamp.

The pending-row retry path proves, via `processor.get_authorization()`, that the
processor already holds the charge. If the service died after that approval and
the borrower retries the next morning, the money was taken yesterday -- but the
row is only being completed now.

Recording `captured_at = now()` would place the capture on the retry date while
the processor's settlement file has it on the original one. Reconciliation
windows on `captured_at`, so that manufactures **two** false findings out of one
crash: a settlement-only break on day N, and a ledger-only break on day N+1.

The fresh-charge branch of the same retry is the opposite case -- it is charging
right now, so our clock IS the capture time and no processor lookup applies.
Both are asserted, because a fix that used the processor's time everywhere would
be wrong in the second case and would look correct in the first.
"""
import pytest

from app import payments, processor


class _FakeDb:
    """Enough of db.query to observe what the capture UPDATE writes."""

    def __init__(self, row):
        self.row = dict(row)
        self.captured_with = None
        self.reference_with = None

    def query(self, sql, params=None):
        stmt = " ".join(sql.split())
        if stmt.startswith("SELECT"):
            return [self.row]
        if "auth_status = 'captured'" in stmt:
            # One form for both capture paths now: authorization id, the
            # processor's capture time, its settlement reference, the row id.
            _auth, captured_at, processor_ref, _pid = params
            self.captured_with = captured_at
            self.reference_with = processor_ref
            self.row["auth_status"] = "captured"
        return []


PROCESSOR_TIME = "2026-08-08T23:58:00+00:00"
PROCESSOR_REF = "PR-100231"


@pytest.fixture
def pending_row():
    return {
        "id": 91, "loan_id": 4471, "amount": 250.00, "auth_status": "pending",
        "applied_at": None, "idempotency_key": "key-crash-recovery",
        "last4": "4242", "brand": "visa",
    }


def test_a_recovered_capture_uses_the_processor_timestamp(monkeypatch, pending_row):
    """The reported defect: the row is completed today, the money moved yesterday."""
    fake = _FakeDb(pending_row)
    monkeypatch.setattr(payments, "db", fake)
    monkeypatch.setattr(payments, "_require_servicing_auth", lambda *a, **k: None)
    monkeypatch.setattr(payments, "_apply_via_servicing", lambda *a, **k: None)
    monkeypatch.setattr(
        processor, "lookup_authorization",
        lambda key: processor.Authorization("auth-existing", PROCESSOR_TIME, PROCESSOR_REF),
    )

    payments.charge(
        loan_id=4471, processor_token="tok_x", amount=250.00,
        last4="4242", brand="visa", idempotency_key="key-crash-recovery",
    )

    assert fake.captured_with == PROCESSOR_TIME, (
        "the recovered capture was stamped with our clock instead of the "
        "processor's. Reconciliation windows on captured_at, so this places the "
        "capture on the retry date and invents a break on both days."
    )
    assert fake.reference_with == PROCESSOR_REF, (
        "the recovered capture did not keep the settlement reference the "
        "processor assigned on the original attempt, so it matches no line in "
        "that day's settlement file"
    )


def test_a_fresh_charge_on_retry_uses_our_clock(monkeypatch, pending_row):
    """The other branch. Nothing was charged before, so there is no processor
    timestamp to inherit -- passing NULL lets the SQL fall back to now()."""
    fake = _FakeDb(pending_row)
    monkeypatch.setattr(payments, "db", fake)
    monkeypatch.setattr(payments, "_require_servicing_auth", lambda *a, **k: None)
    monkeypatch.setattr(payments, "_apply_via_servicing", lambda *a, **k: None)
    monkeypatch.setattr(processor, "lookup_authorization", lambda key: None)
    monkeypatch.setattr(
        processor, "authorize_charge",
        lambda *a, **k: processor.Authorization("auth-new", None, "PR-100999"),
    )

    payments.charge(
        loan_id=4471, processor_token="tok_x", amount=250.00,
        last4="4242", brand="visa", idempotency_key="key-crash-recovery",
    )

    assert fake.captured_with is None, (
        "a charge authorized just now carried a processor timestamp it cannot "
        "have had"
    )
    assert fake.reference_with == "PR-100999", (
        "a charge authorized on the retry stored no settlement reference, so "
        "reconciliation will report it as an unreferenced capture"
    )


def test_a_processor_with_no_timestamp_falls_back_rather_than_failing(monkeypatch, pending_row):
    """Not every processor reports one, and a missing timestamp must not block
    the recovery -- the fallback is the previous behaviour, which is the best
    estimate available."""
    fake = _FakeDb(pending_row)
    monkeypatch.setattr(payments, "db", fake)
    monkeypatch.setattr(payments, "_require_servicing_auth", lambda *a, **k: None)
    monkeypatch.setattr(payments, "_apply_via_servicing", lambda *a, **k: None)
    monkeypatch.setattr(
        processor, "lookup_authorization",
        lambda key: processor.Authorization("auth-existing", None, PROCESSOR_REF),
    )

    payments.charge(
        loan_id=4471, processor_token="tok_x", amount=250.00,
        last4="4242", brand="visa", idempotency_key="key-crash-recovery",
    )

    assert fake.captured_with is None      # SQL COALESCEs to now()
    assert fake.row["auth_status"] == "captured"
    # A missing timestamp must not cost the reference too: the two are
    # independent facts, and the row can still be matched to its settlement line.
    assert fake.reference_with == PROCESSOR_REF
