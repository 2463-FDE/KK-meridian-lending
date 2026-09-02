"""`applied_amount` reports what reached the LOAN, not what was asked for.

G-06. Every return path in `charge()` answered `float(row["amount"])` -- the
amount the caller ASKED for -- including the three `failed` returns whose own
comment says "no balance was ever touched". Measured on a running stack before
the fix:

    declined charge   {"status": "failed",  "applied_amount": 123.45}
    apply refused     {"status": "pending", "applied_amount": 999999.0}

both with zero `ledger_entries`, zero `payment_applications` and the loan's
balance unmoved. A caller trusting the field would have told a borrower money had
been applied that the ledger has no record of -- worst on the second, where the
card really was charged and only the apply had not happened.

WHY `applied` IS A TRUTHFUL BASIS, which is the part worth stating rather than
assuming. `applied` is True only when servicing's `apply-payment` returned
success, and servicing returns success only after `balance.apply_payment_once`
has COMMITTED -- the ledger entries and the `payment_applications` row are
written in that one transaction. Every refusal is an error status that raises
instead: `PaymentExceedsAmountOwed` and `PaymentReplayConflict` map to 409,
`AmountIsNotWholeCents` to 400. And the amount is all-or-nothing, because
`waterfall.allocate` refuses an overpayment rather than absorbing part of it
(D14). So True means the money reached the loan, in full; False means it did not.

These cases pin the four states that contract actually has. They do not
manufacture a servicing response that reports success without applying, or a
partial application: neither is reachable in the current design, and a test for
an unreachable state pins nothing while making the next reader believe it does.
The line comment in `charge()` names what would have to change first if partial
application were ever introduced.
"""
import pytest
from fastapi.testclient import TestClient

from app import config, payments
from app.main import app

from .test_charge_flow import (          # noqa: F401 -- fixture used by name
    _VALID_MOCK_TOKEN,
    _FakeServicingResponse,
    fake_db,
)

client = TestClient(app)

#: Anything not matching `tok_mock_<uuid>` is declined by the stub processor with
#: "token not recognized" (app/processor.py::_MOCK_TOKEN_RE).
_DECLINED_TOKEN = "tok_not_recognised"

#: The route authenticates the caller as a recognised service before it looks at
#: anything else; that check has its own coverage in `test_charge_flow.py`.
_HEADERS = {"X-Internal-Token": config.INTERNAL_SERVICE_TOKEN}


@pytest.fixture(autouse=True)
def _servicing_applies_by_default(monkeypatch):
    """Servicing accepts the apply unless a case says otherwise.

    `test_charge_flow.py` has an equivalent autouse stub, but autouse fixtures do
    not cross modules -- without this one `_apply_via_servicing` attempts a real
    HTTP call, fails, and every case here would report `pending`, which would
    make the negative cases pass for the wrong reason. The cases that need the
    apply to fail install their own stub, so this default can never hide the
    behaviour it stands in for.
    """
    monkeypatch.setattr(payments.httpx, "post",
                        lambda *a, **k: _FakeServicingResponse())


def _payload(**overrides):
    body = {
        "loan_id": 42,
        "processor_token": _VALID_MOCK_TOKEN,
        "last4": "1111",
        "brand": "visa",
        "amount": 250.0,
        "idempotency_key": "g06-key",
    }
    body.update(overrides)
    return body


def _charge(**overrides):
    return client.post("/payments", json=_payload(**overrides), headers=_HEADERS)


def _assert_status_and_amount_agree(body):
    """The two fields a consumer reads together, checked together.

    "captured" is set only when the apply committed, so a non-zero amount on any
    other status -- or a zero on "captured" -- is the disagreement this whole
    change exists to remove.
    """
    if body["status"] == "captured":
        assert body["applied_amount"] > 0, (
            f"captured means the apply committed, so something reached the loan: {body}")
    else:
        assert body["applied_amount"] == 0.0, (
            f"{body['status']!r} means the loan did not move: {body}")


# ---------------------------------------------------------------------------
# 1. Declined: the processor refused, nothing was posted
# ---------------------------------------------------------------------------

def test_a_declined_charge_reports_nothing_applied(fake_db):
    """The old value here was the full requested amount, on the one status whose
    documented meaning is that nothing happened."""
    body = _charge(processor_token=_DECLINED_TOKEN, amount=123.45,
                   idempotency_key="g06-declined").json()

    assert body["status"] == "failed"
    assert body["applied_amount"] == 0.0
    _assert_status_and_amount_agree(body)


def test_replaying_a_declined_key_still_reports_nothing_applied(fake_db):
    """The retry path has its own `failed` return and had the same defect.

    A caller polling a declined key would otherwise have seen a non-zero applied
    amount on every poll.
    """
    key = "g06-declined-replay"
    first = _charge(processor_token=_DECLINED_TOKEN, amount=77.0, idempotency_key=key).json()
    second = _charge(processor_token=_DECLINED_TOKEN, amount=77.0, idempotency_key=key).json()

    assert first["payment_id"] == second["payment_id"], "a second payment row was written"
    for body in (first, second):
        assert body["status"] == "failed"
        assert body["applied_amount"] == 0.0
        _assert_status_and_amount_agree(body)


# ---------------------------------------------------------------------------
# 2. Captured, but the apply did not commit
# ---------------------------------------------------------------------------

def test_a_capture_whose_apply_is_refused_reports_nothing_applied(fake_db, monkeypatch):
    """The case that matters most: the card was charged, the loan was not.

    Servicing refuses an overpayment with 409 (`PaymentExceedsAmountOwed`), so
    `raise_for_status` raises and the apply never commits. The money really did
    leave the borrower, which is why reporting the requested figure here was the
    worst of the three.
    """
    def _refused(*a, **k):
        class _Resp:
            def raise_for_status(self):
                raise RuntimeError("409 payment exceeds amount owed")
        return _Resp()

    monkeypatch.setattr(payments.httpx, "post", _refused)

    body = _charge(amount=999_999.00, idempotency_key="g06-refused").json()

    assert body["status"] == "pending", body
    assert body["applied_amount"] == 0.0
    _assert_status_and_amount_agree(body)


def test_a_capture_whose_apply_errors_reports_nothing_applied(fake_db, monkeypatch):
    """A timeout or connection failure is not an application either.

    Distinct from the refusal above: there the loan was evaluated and declined,
    here nothing is known. Both report zero, and the payment stays reconcilable
    rather than being reported as settled.
    """
    def _boom(*a, **k):
        raise RuntimeError("servicing unreachable")

    monkeypatch.setattr(payments.httpx, "post", _boom)

    body = _charge(amount=30.0, idempotency_key="g06-errored").json()

    assert body["status"] == "pending"
    assert body["applied_amount"] == 0.0
    _assert_status_and_amount_agree(body)


# ---------------------------------------------------------------------------
# 3. Applied: the amount that actually moved
# ---------------------------------------------------------------------------

def test_an_applied_payment_reports_the_amount_that_moved(fake_db):
    """The control. A field that always answered zero would pass every case above."""
    body = _charge(amount=60.0, idempotency_key="g06-applied").json()

    assert body["status"] == "captured"
    assert body["applied_amount"] == 60.0
    _assert_status_and_amount_agree(body)


def test_the_reported_figure_is_the_persisted_one_not_the_raw_request(fake_db):
    """Cents, not the caller's float.

    `charge()` quantises the amount before persisting it, so reporting the
    request back would reintroduce exactly the rounding the quantisation exists
    to remove.
    """
    # Amount and key on separate lines: gitleaks' generic-api-key rule scored the
    # combined literal as high-entropy and failed CI on a test fixture. Splitting
    # them keeps the float exact and the key boring.
    malformed_float = 19.999999999999996
    body = _charge(amount=malformed_float, idempotency_key="quantised").json()

    assert body["status"] == "captured"
    assert body["applied_amount"] == 20.0
    persisted = fake_db.calls[0][1][3]
    assert body["applied_amount"] == persisted
    _assert_status_and_amount_agree(body)


# ---------------------------------------------------------------------------
# 4. The retry path
# ---------------------------------------------------------------------------

def test_an_idempotent_replay_reports_the_same_amount_and_applies_once(fake_db):
    """Exactly-once application, and a replay that neither doubles nor zeroes it."""
    key = "g06-replay-applied"
    first = _charge(amount=45.0, idempotency_key=key).json()
    second = _charge(amount=45.0, idempotency_key=key).json()

    assert first["payment_id"] == second["payment_id"], "a second payment row was written"
    assert first["applied_amount"] == second["applied_amount"] == 45.0
    inserts = [c for c in fake_db.calls if c[0].strip().startswith("INSERT")]
    assert len(inserts) == 2, (
        "the replay should still attempt the guarded INSERT; the partial unique "
        "index is what makes it a no-op")
    for body in (first, second):
        _assert_status_and_amount_agree(body)


def test_a_pending_payment_that_later_applies_starts_reporting_it(fake_db, monkeypatch):
    """The transition a consumer polling one idempotency key actually sees.

    Zero while the apply has not committed, the amount once it has -- the point
    at which "requested" and "applied" diverge most visibly.
    """
    def _boom(*a, **k):
        raise RuntimeError("servicing unreachable")

    monkeypatch.setattr(payments.httpx, "post", _boom)
    unapplied = _charge(amount=30.0, idempotency_key="g06-recovers").json()
    assert unapplied["status"] == "pending"
    assert unapplied["applied_amount"] == 0.0

    monkeypatch.setattr(payments.httpx, "post",
                        lambda *a, **k: _FakeServicingResponse())
    recovered = _charge(amount=30.0, idempotency_key="g06-recovers").json()

    assert recovered["payment_id"] == unapplied["payment_id"]
    assert recovered["status"] == "captured"
    assert recovered["applied_amount"] == 30.0, (
        "the payment applied on the retry but still reported nothing applied")
    _assert_status_and_amount_agree(recovered)
