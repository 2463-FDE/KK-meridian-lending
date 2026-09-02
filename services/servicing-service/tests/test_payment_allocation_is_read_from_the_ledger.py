"""What a payment paid, read from the ledger rather than recomputed.

The client asked at the 2026-08-19 demo whether a borrower can tell where their
payment went. The backend has always known: `apply_payment_once` writes one
`ledger_entries` row per component it moved, keyed `(payment_id, component)`.
Nothing read them back, so the API returned amount, method and date only.

**The temptation this file exists to rule out is recomputation.** Calling
`waterfall.allocate` at read time would look equivalent and produce a second
opinion about a movement that already happened. The two agree only while nothing
has changed since -- and a waived fee, an approved adjustment or a corrected
schedule all change what the waterfall would say today about a payment applied
last month. The borrower would then be shown an allocation that never occurred,
with the real one sitting in the ledger next to it.

So the tests below are built to fail against a recomputing implementation, not
merely to check that three numbers come back. `test_the_split_reports_the_ledger
_even_when_a_recomputation_would_disagree` is the one that matters: it stores an
allocation the waterfall would never produce and asserts the API reports the
stored one.

No allocation policy is invented here. Fees -> interest -> principal is decided
in `waterfall.py` and unchanged; this is a read.
"""
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import models
from app.database import get_session
from app.main import app


class _Session:
    """Answers exactly the two reads the endpoint makes.

    Deliberately not a real session: this is about which rows the endpoint asks
    for and what it does with them, and a database of its own would put the
    interesting assertion behind a fixture nobody reads.
    """

    def __init__(self, payments, ledger_rows):
        self._payments = payments
        self._ledger_rows = ledger_rows

    def scalars(self, _stmt):
        class _R:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return self._rows

        return _R(self._payments)

    def execute(self, _stmt):
        class _R:
            def __init__(self, rows):
                self._rows = rows

            def all(self):
                return self._rows

        return _R(self._ledger_rows)


def _payment(payment_id=1, amount=120.0):
    p = models.Payment()
    p.id = payment_id
    p.loan_id = 1
    p.amount = amount
    p.method = "card"
    p.last4 = "1111"
    p.brand = "visa"
    p.created_at = None
    return p


def _client(payments, ledger_rows):
    app.dependency_overrides[get_session] = lambda: _Session(payments, ledger_rows)
    client = TestClient(app)
    return client


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.clear()


def _items(payments, ledger_rows):
    resp = _client(payments, ledger_rows).get("/loans/1/payments")
    assert resp.status_code == 200, resp.text
    return resp.json()["items"]


# --------------------------------------------------------------------------
# The read.
# --------------------------------------------------------------------------

def test_a_split_payment_reports_all_three_components():
    """Ledger amounts are NEGATIVE deltas -- that is what a payment does to a
    balance. The borrower is shown what they paid, so the sign flips here."""
    items = _items(
        [_payment(1, 120.0)],
        [(1, "fees", Decimal("-10.00")),
         (1, "interest", Decimal("-30.00")),
         (1, "principal", Decimal("-80.00"))],
    )

    assert items[0]["applied_to_fees"] == 10.0
    assert items[0]["applied_to_interest"] == 30.0
    assert items[0]["applied_to_principal"] == 80.0


def test_the_components_sum_to_the_payment():
    """The property that makes the answer trustworthy at a glance. If a reader
    can add the three numbers and not reach the amount, the screen is worse than
    no breakdown at all."""
    items = _items(
        [_payment(1, 120.0)],
        [(1, "fees", Decimal("-10.00")),
         (1, "interest", Decimal("-30.00")),
         (1, "principal", Decimal("-80.00"))],
    )
    item = items[0]

    total = (item["applied_to_fees"] + item["applied_to_interest"]
             + item["applied_to_principal"])
    assert total == pytest.approx(item["amount"])


def test_a_payment_that_only_cleared_fees_reports_zero_for_the_rest():
    """A component the ledger did not record is 0.00 for a payment that HAS
    entries -- the ledger refuses a zero row by CHECK, so absence there means
    'nothing moved', which is a known answer rather than an unknown one."""
    items = _items([_payment(1, 10.0)], [(1, "fees", Decimal("-10.00"))])

    assert items[0]["applied_to_fees"] == 10.0
    assert items[0]["applied_to_interest"] == 0.0
    assert items[0]["applied_to_principal"] == 0.0


def test_a_payment_with_no_ledger_entries_reports_unknown_not_zero():
    """The distinction the borrower's screen depends on.

    A row applied before the ledger existed has no allocation to report. Zero
    would assert 'nothing went to interest'; the truth is 'we do not know', and
    those are different answers to put in front of someone.
    """
    items = _items([_payment(7, 50.0)], [])

    assert items[0]["applied_to_fees"] is None
    assert items[0]["applied_to_interest"] is None
    assert items[0]["applied_to_principal"] is None


def test_each_payment_gets_its_own_split():
    """Guard the guard: one shared allocation applied to every row would satisfy
    a single-payment test and be wrong on every real loan."""
    items = _items(
        [_payment(1, 100.0), _payment(2, 60.0)],
        [(1, "principal", Decimal("-100.00")),
         (2, "fees", Decimal("-20.00")),
         (2, "principal", Decimal("-40.00"))],
    )
    by_id = {i["id"]: i for i in items}

    assert by_id[1]["applied_to_fees"] == 0.0
    assert by_id[1]["applied_to_principal"] == 100.0
    assert by_id[2]["applied_to_fees"] == 20.0
    assert by_id[2]["applied_to_principal"] == 40.0


# --------------------------------------------------------------------------
# The part that makes this a read and not a second waterfall.
# --------------------------------------------------------------------------

def test_the_split_reports_the_ledger_even_when_a_recomputation_would_disagree():
    """The load-bearing case.

    These entries are NOT what today's waterfall would produce for a 120.00
    payment -- fees first would never leave 100.00 on principal while 20.00 of
    interest was owed. They are what the ledger says happened, and a payment
    applied months ago against different arrears legitimately looks like this.

    An implementation that recomputed the allocation would return the tidy
    answer and fail here. That is the whole point: the borrower is shown what
    occurred, not what would occur if the payment arrived today.
    """
    items = _items(
        [_payment(1, 120.0)],
        [(1, "interest", Decimal("-20.00")),
         (1, "principal", Decimal("-100.00"))],
    )

    assert items[0]["applied_to_fees"] == 0.0
    assert items[0]["applied_to_interest"] == 20.0
    assert items[0]["applied_to_principal"] == 100.0


def test_the_read_model_still_exposes_no_card_data():
    """The allocation fields are new surface on a payment response, and the
    boundary they sit on is the one PR #51 traced. Re-asserted here rather than
    assumed: a read path can leak what no write path ever stored.

    An EXACT set, not a subset, on purpose: this is an allowlist, so a field
    added to the response has to be admitted here deliberately rather than
    arriving unnoticed. `auth_status` and `applied` were admitted that way --
    they say WHY an allocation is absent (captured-but-unapplied, versus a legacy
    row with no ledger evidence, versus declined), which payment history needs in
    order to stop telling a borrower that a payment captured seconds ago is
    "not available". Neither carries instrument data: one is the processor's
    authorization state ('pending' | 'captured' | 'failed'), the other a boolean
    over `payments.applied_at`.
    """
    from app import schemas

    assert set(schemas.PaymentItem.model_fields) == {
        "id", "amount", "method", "masked_pan", "created_at",
        "applied_to_fees", "applied_to_interest", "applied_to_principal",
        "auth_status", "applied",
    }


def test_the_query_asks_only_for_payment_entries():
    """The filter, asserted on the statement -- because the database applies it.

    A fake session cannot show this: it returns whatever rows the test hands it,
    so removing the `entry_type` predicate changes nothing above and every case
    still passes. That gap was found by mutation rather than by reading, and the
    predicate matters: a fee assessment, a waiver and an approved adjustment all
    move the same components on the same loan, so without it the borrower would
    be told they PAID money that was charged to them or written off for them.

    `payment_id IS NOT NULL` is asserted for the same reason -- a `fee_assessed`
    entry has no payment behind it and must never be grouped under one.
    """
    from sqlalchemy.dialects import postgresql

    from app.routers import loans as loans_router

    captured = []

    class _Recorder:
        def execute(self, stmt):
            captured.append(stmt)

            class _R:
                def all(self):
                    return []

            return _R()

    loans_router._allocations_by_payment(_Recorder(), 1)

    sql = str(captured[0].compile(dialect=postgresql.dialect(),
                                  compile_kwargs={"literal_binds": True}))
    assert "entry_type = 'payment'" in sql, sql
    assert "payment_id IS NOT NULL" in sql, sql
    assert "loan_id = 1" in sql, sql
