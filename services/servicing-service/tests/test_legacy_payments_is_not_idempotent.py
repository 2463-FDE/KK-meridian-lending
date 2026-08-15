"""Characterization: servicing's legacy `POST /payments` is not idempotent.

This is D2's open half, and this file proves it rather than describing it.
`docs/DEBT.md` D2 and `docs/ROADMAP.md`'s Week 5 row both say a retry
double-records the payment and double-applies the balance; before this test, the
only evidence for that was a comment, and comments in this repository have been
wrong in both directions often enough to have their own register entry (D5c).

**It documents the defect. It does not fix it.** Every assertion below is written
so that it FAILS when the route becomes idempotent or is deleted, and that is the
point: the day someone closes D2, this file and the two documents that describe
it have to be rewritten in the same change. A characterization test that quietly
kept passing after the fix would let the register go stale again, which is the
exact failure this pass exists to correct.

**What is duplicated, and what is not.** Two `payments` rows and two balance
applications. Nothing is authorized twice: this route calls no processor at all,
so the borrower's card is untouched. That is why its rows carry
`capture_source='servicing_legacy'` and are excluded from reconciliation (D7) --
there is no settlement line that could corroborate either copy. The distinction
matters because payment-service's fixed path DID have a real double-charge, and
collapsing the two names the wrong fix.

The route is reachable only with `X-Internal-Token`, and the gateway 404s the
path rather than proxying it. Bounded, not closed.

No PostgreSQL: `db.query` is recorded, and `balance.apply_payment` is counted.
"""
import pytest
from fastapi.testclient import TestClient

from app import main, payments


TOKEN = "test-internal-token"

#: One request. Sent twice, unchanged -- the ordinary shape of a client retry
#: after a timeout, which is the case idempotency exists for.
BODY = {
    "loan_id": 4471,
    "processor_token": "tok_visa_x",
    "last4": "1111",
    "brand": "visa",
    "amount": 250.00,
    "method": "card",
}


@pytest.fixture
def legacy_route(monkeypatch):
    """The legacy charge path with its database and balance layer recorded."""
    monkeypatch.setattr(main.config, "INTERNAL_SERVICE_TOKEN", TOKEN)

    inserts = []
    applies = []

    def _query(sql, params=None):
        if "INSERT INTO payments" in sql:
            inserts.append(params)
        return []

    def _apply_payment(loan_id, amount):
        applies.append((loan_id, amount))
        return 0.0

    monkeypatch.setattr(payments.db, "query", _query)
    monkeypatch.setattr(payments.balance, "apply_payment", _apply_payment)
    return inserts, applies


def _post(body=None):
    return TestClient(main.app).post(
        "/payments", json=body or BODY, headers={"X-Internal-Token": TOKEN},
    )


def test_a_retried_request_records_the_payment_twice(legacy_route):
    """Two identical requests, two `payments` rows.

    Fails when the route learns to deduplicate -- at which point D2's open half
    is closed and this file is the wrong description of the system.
    """
    inserts, _ = legacy_route

    first, second = _post(), _post()

    assert first.status_code == 200 and second.status_code == 200, (
        "the retry was refused -- if that is deliberate, D2's open half has "
        "changed and docs/DEBT.md D2 needs rewriting with this test"
    )
    assert len(inserts) == 2, (
        f"expected the documented defect -- two payments rows from two identical "
        f"requests -- but {len(inserts)} INSERT(s) reached the database. If the "
        f"route is now idempotent, close D2's open half and delete this test."
    )


def test_a_retried_request_applies_the_balance_twice(legacy_route):
    """The half that costs the borrower money.

    A duplicate row is a reporting problem; a second `apply_payment` is a loan
    balance that no longer reflects what was paid.
    """
    _, applies = legacy_route

    _post()
    _post()

    assert applies == [(4471, 250.00), (4471, 250.00)], (
        f"expected the same amount applied to the same loan twice, got {applies!r}"
    )


def test_no_processor_is_called_so_nothing_is_charged_twice(legacy_route):
    """The claim the documents make about what is NOT wrong here.

    `charge()` takes a `processor_token` and does nothing with it: no
    authorization, no capture, no processor module imported at all. So a retry
    duplicates the record and the balance movement and never touches the card.
    Asserted because the wording it protects -- "double-records and
    double-applies", not "double-charges" -- is only accurate while this holds.
    """
    import inspect

    source = inspect.getsource(payments)
    for processor_call in ("authorize_charge(", "capture_charge(", "processor."):
        assert processor_call not in source, (
            f"servicing's legacy charge() now references {processor_call!r}. If it "
            f"calls a processor, a retry IS a double-charge and every document "
            f"describing this route needs the stronger word back."
        )
    assert not hasattr(payments, "processor"), (
        "a processor module is now in scope in servicing's legacy payments.py"
    )
    # And the row it writes says so about itself. `capture_source` is the label
    # reconciliation uses to exclude these rows -- note the substring "capture"
    # here is that column name, not a capture call, which is why the checks above
    # match call syntax rather than the bare word.
    assert "'servicing_legacy'" in source, (
        "the capture_source label is gone, so reconciliation can no longer tell "
        "these rows from processor-backed ones (D7)"
    )


def test_the_route_accepts_no_idempotency_key(legacy_route):
    """A caller cannot opt into safety, and cannot be misled into thinking it has.

    Two facts, and the second is the redeeming one. `PaymentIn` declares no
    `idempotency_key`, so there is no protection to ask for -- but it is also
    `extra="forbid"` (ADR 0008, for the raw PAN/CVV fields it used to accept), so
    a client that sends one is refused with 422 rather than being silently
    ignored. A silently dropped key would be the worse defect of the two: a
    caller believing it was protected, with no error to tell it otherwise.

    So the accurate statement, and the one the documents now make, is that this
    route has no idempotency at all -- not that it pretends to.
    """
    inserts, applies = legacy_route

    assert "idempotency_key" not in main.PaymentIn.model_fields, (
        "the legacy route now declares an idempotency_key -- D2's open half may "
        "be closing; update docs/DEBT.md D2, the Week 5 roadmap row and this "
        "test together"
    )

    keyed = dict(BODY, idempotency_key="retry-key-1")
    refused = _post(keyed)
    assert refused.status_code == 422, (
        f"a body-supplied idempotency key was accepted with "
        f"{refused.status_code} -- if the route now honours one, D2's open half "
        f"has changed; if it merely ignores it, that is a new and worse defect"
    )
    assert not inserts and not applies, (
        "the refused request still reached the database or the balance layer"
    )
