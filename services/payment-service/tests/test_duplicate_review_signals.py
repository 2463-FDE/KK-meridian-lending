"""The client's duplicate-review contract, tested as they wrote it.

Client decision, 2026-08-24, in their words:

    "Flag qualifying payments for human reconciliation review. Do not treat the
     flag as a duplicate or validity conclusion or as permission to move money."

    Exact duplicate: "if the provider transaction ID or payment idempotency key
    is identical, treat it as an exact-duplicate signal regardless of elapsed
    time."

    Heuristic: "flag ONLY when the candidate has: same loan, same amount, same
    payment source, same payment channel, within a rolling 30-minute window.
    Same loan + same amount alone is NOT sufficient. Distinct legitimate
    scheduled or installment payments must remain possible."

So the tests below are organised around the four factors and the window, and the
false-positive cases matter as much as the positive one: a control that flags a
borrower's legitimate second installment is how a queue teaches operators to stop
reading it -- the failure the reconciliation rewrite already had to undo once (D7).

The predicate is pure, which is what makes the boundary testable to the second
without a database or a clock.

Synthetic data only: fictional loans, fictional amounts, and `src_mock_*` handles
of exactly the shape the mock tokenizer mints.
"""
import datetime

import pytest

from app import review_signals

NOW = datetime.datetime(2026, 8, 24, 12, 0, 0, tzinfo=datetime.timezone.utc)


def capture(**over):
    """A captured payment row, as `payments` holds one."""
    row = {
        "id": 1,
        "loan_id": 4471,
        "amount": "250.00",
        "method": "card",
        "source_ref": "src_mock_11111111-1111-4111-8111-111111111111",
        "captured_at": NOW,
        "correlation_id": "pay_deadbeef",
    }
    row.update(over)
    return row


# --------------------------------------------------------------------------
# The four factors. Each of these is a false positive if the rule is loosened.
# --------------------------------------------------------------------------

def test_all_four_factors_inside_the_window_is_a_candidate():
    earlier = capture(id=1, captured_at=NOW - datetime.timedelta(minutes=5))
    candidate = capture(id=2)

    assert review_signals.heuristic_matches(candidate, earlier)


def test_same_loan_and_amount_alone_is_not_a_candidate():
    """The client said so in as many words, and it is the whole reason the other
    three factors exist: two payments of the same size on one loan is what a
    second installment looks like."""
    earlier = capture(id=1, source_ref="src_mock_aaaa", method="ach",
                      captured_at=NOW - datetime.timedelta(minutes=5))
    candidate = capture(id=2, source_ref="src_mock_bbbb", method="card")

    assert not review_signals.heuristic_matches(candidate, earlier)


def test_a_different_source_is_not_a_candidate():
    earlier = capture(id=1, source_ref="src_mock_aaaa",
                      captured_at=NOW - datetime.timedelta(minutes=1))
    candidate = capture(id=2, source_ref="src_mock_bbbb")

    assert not review_signals.heuristic_matches(candidate, earlier)


def test_a_different_channel_is_not_a_candidate():
    earlier = capture(id=1, method="ach",
                      captured_at=NOW - datetime.timedelta(minutes=1))
    candidate = capture(id=2, method="card")

    assert not review_signals.heuristic_matches(candidate, earlier)


def test_a_different_loan_is_not_a_candidate():
    earlier = capture(id=1, loan_id=5582,
                      captured_at=NOW - datetime.timedelta(minutes=1))
    candidate = capture(id=2, loan_id=4471)

    assert not review_signals.heuristic_matches(candidate, earlier)


def test_a_different_amount_is_not_a_candidate():
    earlier = capture(id=1, amount="250.01",
                      captured_at=NOW - datetime.timedelta(minutes=1))
    candidate = capture(id=2, amount="250.00")

    assert not review_signals.heuristic_matches(candidate, earlier)


def test_amounts_are_compared_as_decimals():
    """`0.1 + 0.2 != 0.3`, and this repository moved its money to NUMERIC for
    that reason (D12). A float comparison here would be a silent false negative
    in a control whose job is noticing a resemblance."""
    earlier = capture(id=1, amount=250.00,
                      captured_at=NOW - datetime.timedelta(minutes=1))
    candidate = capture(id=2, amount="250.000")

    assert review_signals.heuristic_matches(candidate, earlier)


# --------------------------------------------------------------------------
# Unknown is not a match. This is the client's "insufficient evidence" rule.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("missing", [None, ""])
def test_an_unknown_source_on_the_candidate_is_never_a_match(missing):
    """An ACH payment has no tokenizer and a pre-0044 capture has no handle.
    Falling back to loan + amount + channel is precisely the false positive the
    source factor exists to prevent, so unknown must fail closed -- toward NOT
    flagging."""
    earlier = capture(id=1, captured_at=NOW - datetime.timedelta(minutes=1))
    candidate = capture(id=2, source_ref=missing)

    assert not review_signals.heuristic_matches(candidate, earlier)


def test_an_unknown_source_on_the_earlier_payment_is_never_a_match():
    earlier = capture(id=1, source_ref=None,
                      captured_at=NOW - datetime.timedelta(minutes=1))
    candidate = capture(id=2)

    assert not review_signals.heuristic_matches(candidate, earlier)


def test_a_missing_capture_time_is_never_a_match():
    """An authorization that never confirmed has no capture instant. Treating a
    missing timestamp as "now" would make the window depend on when the query
    ran."""
    assert not review_signals.heuristic_matches(
        capture(id=2, captured_at=None),
        capture(id=1, captured_at=NOW - datetime.timedelta(minutes=1)))
    assert not review_signals.heuristic_matches(
        capture(id=2), capture(id=1, captured_at=None))


def test_a_payment_is_not_its_own_candidate():
    assert not review_signals.heuristic_matches(capture(id=7), capture(id=7))


# --------------------------------------------------------------------------
# The window, pinned to the second.
# --------------------------------------------------------------------------

def test_twenty_nine_fifty_nine_is_inside_the_window():
    earlier = capture(id=1, captured_at=NOW - datetime.timedelta(minutes=29, seconds=59))
    candidate = capture(id=2)

    assert review_signals.heuristic_matches(candidate, earlier)


def test_exactly_thirty_minutes_is_inside_the_window():
    """The documented interpretation: INCLUSIVE at 30:00.

    "Within a rolling 30-minute window" reads as including the boundary, and
    `review_signals.WINDOW_IS_INCLUSIVE` records that choice next to the
    comparison so the query and this test cannot disagree about it.
    """
    earlier = capture(id=1, captured_at=NOW - datetime.timedelta(minutes=30))
    candidate = capture(id=2)

    assert review_signals.WINDOW_IS_INCLUSIVE
    assert review_signals.heuristic_matches(candidate, earlier)


def test_thirty_minutes_and_one_second_is_outside_the_window():
    earlier = capture(id=1, captured_at=NOW - datetime.timedelta(minutes=30, seconds=1))
    candidate = capture(id=2)

    assert not review_signals.heuristic_matches(candidate, earlier)


def test_the_window_is_rolling_not_anchored_to_a_clock():
    """Two captures 20 minutes apart match wherever they sit in the hour, so a
    pair straddling the top of an hour is treated the same as a pair inside
    one."""
    across_the_hour = capture(id=1, captured_at=NOW.replace(hour=11, minute=50))
    candidate = capture(id=2, captured_at=NOW.replace(hour=12, minute=10))

    assert review_signals.heuristic_matches(candidate, across_the_hour)


def test_order_does_not_change_the_window_answer():
    """`is_within_window` is symmetric: a clock skew that puts the earlier
    capture microseconds after the later one must not silently disable the
    check."""
    a = NOW
    b = NOW + datetime.timedelta(minutes=10)

    assert review_signals.is_within_window(a, b)
    assert review_signals.is_within_window(b, a)


# --------------------------------------------------------------------------
# A legitimate installment stays possible. The client asked for this explicitly.
# --------------------------------------------------------------------------

def test_two_scheduled_installments_a_month_apart_are_not_flagged():
    """Same loan, same amount, same card, same channel -- and a month apart,
    which is what an installment plan looks like."""
    last_month = capture(id=1, captured_at=NOW - datetime.timedelta(days=30))
    candidate = capture(id=2)

    assert not review_signals.heuristic_matches(candidate, last_month)


def test_two_payments_the_same_day_from_different_cards_are_not_flagged():
    """A borrower paying twice in an afternoon from two cards is not a
    duplicate, and the source factor is what tells them apart."""
    earlier = capture(id=1, source_ref="src_mock_card_a",
                      captured_at=NOW - datetime.timedelta(minutes=3))
    candidate = capture(id=2, source_ref="src_mock_card_b")

    assert not review_signals.heuristic_matches(candidate, earlier)
