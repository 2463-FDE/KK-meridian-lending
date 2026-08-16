"""Servicing's processorless `POST /payments` is gone, and cannot come back quietly.

This replaces `test_legacy_payments_is_not_idempotent.py`, which characterized
the defect: two identical requests produced two `payments` rows and two
`balance.apply_payment` calls. That test was correct and is now wrong, because
the behaviour it described no longer exists. Both files could not be true at
once, which is the point of the swap -- a characterization test outlives its
subject unless retiring the subject retires it.

**What is asserted here**

  1. the route is absent, and a call reaches no money code at all;
  2. the module that performed the charge is gone, not merely unreferenced;
  3. a retry cannot double-record or double-apply through servicing, because
     there is no servicing path that records a payment;
  4. the servicing-side apply path that DOES remain is still idempotent by
     `payment_id` -- retiring the duplicate must not weaken the real one;
  5. historical `capture_source='servicing_legacy'` rows are still a supported
     value, so reconciliation keeps excluding them rather than failing on them.

**What is deliberately NOT claimed.** D2 is closed for payment *creation*: the
only path that records a payment is payment-service's, which requires an
`idempotency_key`. This says nothing about `balance.apply_payment`, which is now
unreferenced by any route but still present for `tests/test_money.py`'s Decimal
evidence -- removing it belongs with the ledger writer conversion (ADR 0010
steps 3 and 5), not with retiring an endpoint.
"""
import importlib

import pytest
from fastapi.testclient import TestClient

from app import main


TOKEN = "test-internal-token"

#: The exact request the retired route used to accept. Sent twice below, because
#: "a retry double-records" was the defect and its absence is what is proven.
LEGACY_BODY = {
    "loan_id": 4471,
    "processor_token": "tok_visa_x",
    "last4": "1111",
    "brand": "visa",
    "amount": 250.00,
    "method": "card",
}


@pytest.fixture(autouse=True)
def token(monkeypatch):
    monkeypatch.setattr(main.config, "INTERNAL_SERVICE_TOKEN", TOKEN)


@pytest.fixture
def tripwires(monkeypatch):
    """Explodes if a request reaches the database or the balance layer.

    Status-code assertions alone would pass on a handler that wrote the row and
    then returned 404, which is the outcome that actually matters: the balance
    does not un-move because the response was an error.
    """
    def _boom(*a, **kw):                                     # pragma: no cover
        raise AssertionError(
            "a call to the retired /payments route reached money code"
        )

    monkeypatch.setattr(main.db, "query", _boom)
    for fn in ("apply_payment", "apply_payment_once", "adjust_balance", "waive_fee"):
        monkeypatch.setattr(main.balance, fn, _boom, raising=False)
    return _boom


def _client():
    return TestClient(main.app)


def test_the_route_is_absent_from_the_application():
    """Asserted against the app's own routing table, not against a response.

    A 404 can also mean "wrong path spelled in the test". This names the
    condition directly: no POST route serves /payments.
    """
    payment_posts = [
        route.path for route in main.app.routes
        if getattr(route, "path", None) == "/payments"
        and "POST" in getattr(route, "methods", set())
    ]
    assert not payment_posts, (
        f"POST /payments is registered again: {payment_posts}. The processorless "
        f"duplicate was retired; payment-service is the only path that may "
        f"record a payment."
    )


def test_a_call_is_refused_and_touches_no_money_code(tripwires):
    response = _client().post(
        "/payments", json=LEGACY_BODY, headers={"X-Internal-Token": TOKEN}
    )
    assert response.status_code in (404, 405), (
        f"the retired route answered {response.status_code}; it must not be "
        f"routable at all"
    )


def test_a_retry_cannot_double_record_or_double_apply(tripwires):
    """The defect, asserted as absent rather than characterized.

    The old test sent this same request twice and counted two inserts and two
    balance applications. Now both calls are refused before any money code runs,
    and the `tripwires` fixture -- not the status code -- is what proves it.
    """
    client = _client()
    first = client.post("/payments", json=LEGACY_BODY, headers={"X-Internal-Token": TOKEN})
    second = client.post("/payments", json=LEGACY_BODY, headers={"X-Internal-Token": TOKEN})

    assert first.status_code in (404, 405) and second.status_code in (404, 405), (
        f"retry pair answered {first.status_code}/{second.status_code}"
    )


def test_the_charge_module_is_gone_not_merely_unused():
    """An unreferenced module is one import away from being referenced again.

    `app/payments.py` held the insert and the unconditional
    `balance.apply_payment` call. It is deleted, so re-adding the route means
    re-writing the charge, which is a change a reviewer sees.
    """
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("app.payments")


def test_the_remaining_apply_path_is_still_idempotent_by_payment_id():
    """Retiring the duplicate must not weaken the real one.

    `POST /accounts/{loan_id}/apply-payment` is the path payment-service calls
    after it captures a charge, and its idempotency comes from
    `balance.apply_payment_once`'s primary-key guard on `payment_id`. Asserted
    here as a contract check -- the behaviour itself is proven against real
    PostgreSQL in `test_apply_payment_idempotency.py`.
    """
    import inspect

    from app import balance

    source = inspect.getsource(balance.apply_payment_once)
    assert "ON CONFLICT (payment_id) DO NOTHING" in source, (
        "apply_payment_once no longer guards on payment_id -- the servicing-side "
        "apply became re-appliable while the duplicate route was being retired"
    )
    assert "INSERT INTO ledger_entries" in source, (
        "the apply path no longer writes a ledger entry"
    )


def test_no_servicing_code_writes_the_legacy_capture_label():
    """Nothing may create a `servicing_legacy` row any more.

    The value stays valid in the schema for the rows already carrying it -- see
    the reconciliation test below -- but the writer is gone, so the population is
    now closed.
    """
    import ast
    import pathlib

    # Per STRING LITERAL, not per file. A file-level "contains both words" check
    # flagged `main.py`, which names the label in a comment explaining the
    # retirement and separately runs an unrelated INSERT in the auth-check
    # preflight. The claim is about SQL that writes the label, so the unit has to
    # be the SQL.
    app_dir = pathlib.Path(main.__file__).parent
    offenders = []
    for path in app_dir.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            sql = node.value.upper()
            if "INSERT" in sql and "SERVICING_LEGACY" in sql:
                offenders.append(f"{path.name}:{node.lineno}")
    assert not offenders, (
        f"{offenders} still insert rows labelled servicing_legacy; retiring the "
        f"route was supposed to close that population"
    )


def test_the_legacy_capture_label_is_still_a_permitted_value():
    """Historical rows must keep working.

    Retiring the writer must not invalidate what it wrote: those `payments` rows
    are real money history, reconciliation counts and excludes them, and a
    schema that rejected the value would break every database that has one.
    """
    import pathlib

    schema = (pathlib.Path(main.__file__).parents[3] / "db" / "init"
              / "001_schema.sql").read_text(encoding="utf-8")
    assert "'servicing_legacy'" in schema, (
        "the capture_source CHECK no longer permits servicing_legacy, so every "
        "database holding a historical row from the retired route is now invalid"
    )
