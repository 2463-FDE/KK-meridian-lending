"""What `GET /loans/{id}/activity` reports, and what it refuses to report.

Payment history and account activity answer different questions, and the whole
value of adding a second read model is that it does not blur the first:

  * payment history -- "what payments did I make, and where did each one go?"
  * account activity -- "what authoritative movements changed this account?"

An approved adjustment and a fee waiver change the account without being
payments. A proposal that nobody approved changes nothing and belongs in neither.

The properties below are the ones that decide whether a borrower can trust the
screen: that a payment appears once rather than three times, that the grouping
key is authoritative identity rather than a coincidence of amount and time, that
implementation names and staff-entered text stay server-side, and that reading
the list changes nothing.
"""
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from app import main
from app.database import get_session


class _Row:
    """One `ledger_entries` row in the shape the route's SELECT yields."""

    def __init__(self, id, entry_type, component, amount, payment_id=None,
                 occurred_at=None):
        self.values = (id, entry_type, component, Decimal(str(amount)),
                       payment_id, occurred_at)

    def __iter__(self):
        return iter(self.values)


class _When:
    """A stand-in for a timestamp column: only `.isoformat()` is called."""

    def __init__(self, text):
        self.text = text

    def isoformat(self):
        return self.text


class _FakeSession:
    def __init__(self, rows):
        self._rows = rows
        self.executed = []

    def execute(self, statement):
        self.executed.append(str(statement))
        return self

    def all(self):
        return list(self._rows)


def _activity(rows, loan_id=1):
    def _fake_get_session():
        yield _FakeSession(rows)

    main.app.dependency_overrides[get_session] = _fake_get_session
    try:
        response = TestClient(main.app).get("/loans/%d/activity" % loan_id)
    finally:
        main.app.dependency_overrides.pop(get_session, None)
    assert response.status_code == 200, response.text
    return response.json()


# --- one payment is one movement ----------------------------------------------


def test_a_payment_split_three_ways_is_one_movement():
    """The defect this grouping exists to prevent: a single $500 card charge
    writing three ledger rows and being shown as three charges the borrower
    never made."""
    body = _activity([
        _Row(11, "payment", "fees", "-25.00", payment_id=512, occurred_at=_When("t1")),
        _Row(12, "payment", "interest", "-75.00", payment_id=512, occurred_at=_When("t1")),
        _Row(13, "payment", "principal", "-400.00", payment_id=512, occurred_at=_When("t1")),
    ])

    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["payment_id"] == 512
    assert item["amount"] == -500.00
    assert item["components"] == {"fees": -25.00, "interest": -75.00,
                                  "principal": -400.00}


def test_the_components_sum_to_the_movement():
    """Asserted rather than assumed: the parts are summed in Decimal precisely so
    a cent cannot go missing between the split and the total."""
    body = _activity([
        _Row(1, "payment", "fees", "-0.01", payment_id=7),
        _Row(2, "payment", "interest", "-0.02", payment_id=7),
        _Row(3, "payment", "principal", "-33.33", payment_id=7),
    ])

    item = body["items"][0]
    assert round(sum(item["components"].values()), 2) == item["amount"] == -33.36


def test_interest_appears_even_though_it_projects_to_no_balance():
    """`interest` updates no balance column -- it is owed within a payment, not
    carried (ADR 0010). It is still money the borrower paid, so omitting it here
    would make a payment's parts fail to add up to its whole."""
    body = _activity([
        _Row(1, "payment", "interest", "-75.00", payment_id=9),
        _Row(2, "payment", "principal", "-425.00", payment_id=9),
    ])

    assert "interest" in body["items"][0]["components"]


def test_two_payments_of_the_same_amount_stay_two_movements():
    """The grouping key is authoritative identity, not a coincidence.

    Same loan, same amount, same instant -- and two real payments. Grouping on
    any of those would merge them, which is the same mistake the duplicate-review
    contract (D22) exists because of: same-loan-same-amount is a reason to ask a
    human, never an identity.
    """
    body = _activity([
        _Row(1, "payment", "principal", "-250.00", payment_id=101, occurred_at=_When("t")),
        _Row(2, "payment", "principal", "-250.00", payment_id=102, occurred_at=_When("t")),
    ])

    assert len(body["items"]) == 2
    assert {item["payment_id"] for item in body["items"]} == {101, 102}


def test_entries_with_no_payment_stay_separate_movements():
    """Two adjustments are two movements. There is no payment id to group them
    by, and merging on amount or day would invent one."""
    body = _activity([
        _Row(1, "adjustment", "principal", "450.00"),
        _Row(2, "adjustment", "principal", "450.00"),
    ])

    assert len(body["items"]) == 2
    assert all(item["payment_id"] is None for item in body["items"])
    assert body["items"][0]["id"] != body["items"][1]["id"]


# --- categories, and what never leaves the server -----------------------------


@pytest.mark.parametrize("entry_type,category", [
    ("payment", "payment"),
    ("adjustment", "adjustment"),
    ("fee_assessed", "fee"),
    ("fee_waived", "fee_waiver"),
    ("disbursement", "disbursement"),
    ("opening_balance", "opening_balance"),
])
def test_each_entry_type_maps_to_a_truthful_category(entry_type, category):
    body = _activity([_Row(1, entry_type, "principal", "100.00")])

    assert body["items"][0]["category"] == category


def test_the_legacy_write_mechanism_name_never_reaches_the_caller():
    """`legacy_direct_write` is the name of a mechanism -- a balance change the
    0035 trigger captured from a direct UPDATE predating the ledger. It is
    meaningless to a borrower and alarming to anyone who guesses, and it is real
    data: the seeded database contains one."""
    body = _activity([_Row(1, "legacy_direct_write", "principal", "-100.00")])

    assert "legacy" not in repr(body).lower()
    item = body["items"][0]
    assert item["category"] == "balance_change"
    # And it says the provenance is thin, rather than implying a full record.
    assert item["provenance"] == "limited"


def test_an_unknown_entry_type_falls_back_rather_than_leaking_it():
    """A type added later must not appear raw while nobody has decided how to
    describe it."""
    body = _activity([_Row(1, "some_future_type", "principal", "5.00")])

    assert "some_future_type" not in repr(body)
    assert body["items"][0]["provenance"] == "limited"


def test_no_staff_reason_actor_or_correlation_is_returned():
    """Not by sanitising the output -- by never selecting the columns.

    Staff-entered reason text carries internal operations and compliance
    language, and the only identity this route can see is an unsigned
    `X-User-Role` the gateway forwards. Gating PII on a header a direct caller
    could assert would be the weaker arrangement, so there is one representation
    and it is the safe one.
    """
    rows = [_Row(1, "adjustment", "principal", "450.00")]
    body = _activity(rows)

    for forbidden in ("reason", "actor", "correlation", "actor_id", "actor_role"):
        assert forbidden not in repr(body).lower(), (
            "the activity payload carries %r" % forbidden)


def test_the_query_does_not_select_the_private_columns():
    """The statement itself, because an absent field could equally mean the
    fixture never supplied one."""
    session = _FakeSession([])

    def _fake_get_session():
        yield session

    main.app.dependency_overrides[get_session] = _fake_get_session
    try:
        TestClient(main.app).get("/loans/1/activity")
    finally:
        main.app.dependency_overrides.pop(get_session, None)

    statement = " ".join(session.executed).lower()
    for column in ("reason", "actor_id", "actor_role", "correlation_id"):
        assert column not in statement, (
            "the activity SELECT reads %r; it is never returned, but selecting it "
            "puts it one edit away from being" % column)


# --- the sign convention, which is the information ----------------------------


def test_a_positive_adjustment_reads_as_owing_more():
    """The same convention the adjustment form uses: +450 means the borrower owes
    $450 more. Payment history flips signs for readability; activity must not,
    because the direction IS what the reader came for."""
    body = _activity([_Row(1, "adjustment", "principal", "450.00")])

    assert body["items"][0]["amount"] == 450.00


def test_a_payment_reads_as_owing_less():
    body = _activity([_Row(1, "payment", "principal", "-450.00", payment_id=3)])

    assert body["items"][0]["amount"] == -450.00


# --- what the payload says about itself ---------------------------------------


def test_the_response_says_an_unapproved_proposal_is_not_here():
    """In the payload, not only in the UI. The most likely misreading of this
    list is that it shows everything staff have asked for."""
    body = _activity([])

    assert "moves no money" in body["note"]
    assert "ledger" in body["note"]


def test_an_empty_ledger_is_an_empty_list_not_an_error():
    body = _activity([])

    assert body["items"] == []
    assert body["loan_id"] == 1


# --- reading changes nothing --------------------------------------------------


def test_reading_activity_issues_no_write():
    session = _FakeSession([_Row(1, "payment", "principal", "-1.00", payment_id=1)])

    def _fake_get_session():
        yield session

    main.app.dependency_overrides[get_session] = _fake_get_session
    try:
        TestClient(main.app).get("/loans/1/activity")
    finally:
        main.app.dependency_overrides.pop(get_session, None)

    for statement in session.executed:
        lowered = statement.lower().lstrip()
        assert lowered.startswith("select"), (
            "activity issued a non-SELECT statement: %s" % statement[:200])
