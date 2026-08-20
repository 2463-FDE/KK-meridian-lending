"""Compounding is real, and it is a question rather than a defect.

`docs/DEBT.md` D23. The client asked at the 2026-08-19 demo whether the late fee
is meant to compound. It does: the fee is the lesser of $35 and five per cent of
arrears, a posted fee raises arrears, so the next assessment prices off a base
that already contains the previous fee.

**This file does not say that is wrong.** `policies/fee_schedule.md` publishes
the comparison and the code implements it exactly; what the schedule does not
say is whether repetition is intended, and Lending Operations owns that. These
are characterization tests: they pin what the system does today so the register
entry cannot quietly go stale, and so that whoever answers the question can see
precisely what would change.

If the answer arrives and the behaviour changes, these tests are expected to
fail. That is the point of writing them down -- a decision should break the
description of the old behaviour, loudly, rather than leave two accounts of the
fee in the repository.

Deliberately arithmetic-only: no database, no ledger. The concurrency half of
this area is closed and proved elsewhere with a real two-connection race
(`test_late_fee_goes_through_the_ledger.py`). Mixing the two would blur a closed
engineering defect with an open product question, which is the confusion D23
exists to prevent.
"""
import pathlib
from decimal import Decimal

import pytest

from app.delinquency import LATE_FEE_FLAT, LATE_FEE_PCT_OF_PAST_DUE, late_fee_for

REPO = pathlib.Path(__file__).resolve().parents[3]
FEE_SCHEDULE = REPO / "policies" / "fee_schedule.md"


def test_the_published_rule_is_still_the_lesser_of_the_two():
    """The premise. If the schedule is ever rewritten, these tests describe a
    rule that no longer exists and must be revisited rather than trusted."""
    text = FEE_SCHEDULE.read_text(encoding="utf-8")

    assert "$35 flat, or 5% of the past-due amount, whichever is **less**" in text
    assert LATE_FEE_FLAT == Decimal("35.00")
    assert LATE_FEE_PCT_OF_PAST_DUE == Decimal("0.05")


def test_a_second_assessment_prices_off_arrears_that_include_the_first_fee():
    """The compounding itself, in the range where the percentage governs.

    Two hundred in arrears yields ten. The projection adds that fee to
    `past_due` (`db/migrations/0035_ledger_entries.sql` does
    `past_due = past_due + NEW.amount`), so the next assessment sees 210.
    """
    arrears = Decimal("200.00")

    first = late_fee_for(arrears)
    second = late_fee_for(arrears + first)

    assert first == Decimal("10.00")
    assert second == Decimal("10.50")
    assert second > first, (
        "the second fee did not price off the raised arrears -- if this now "
        "holds, the compounding question in DEBT.md D23 has been answered in "
        "code and the entry needs updating"
    )


def test_the_flat_cap_stops_compounding_above_the_crossover():
    """Bounded, and the bound is worth stating to the client.

    At or above $700 the flat fee is the lesser of the two, so repeated
    assessment charges the same $35 each time. Compounding is a below-crossover
    behaviour, not an unbounded escalation.
    """
    arrears = Decimal("1000.00")

    first = late_fee_for(arrears)
    second = late_fee_for(arrears + first)

    assert first == LATE_FEE_FLAT
    assert second == LATE_FEE_FLAT


def test_no_single_assessment_ever_exceeds_the_published_bounds():
    """Whatever is decided about repetition, each individual fee stays inside
    the rule. This is the part that is NOT in question, separated from the part
    that is."""
    for raw in ("0.25", "12.34", "200.00", "699.99", "700.00", "5000.00"):
        arrears = Decimal(raw)
        fee = late_fee_for(arrears)
        assert fee <= LATE_FEE_FLAT
        assert fee <= (arrears * LATE_FEE_PCT_OF_PAST_DUE).quantize(Decimal("0.01"))


@pytest.mark.parametrize("assessments", [2, 3, 5])
def test_repeated_assessment_below_the_crossover_grows(assessments):
    """What a borrower would actually experience, stated as a number rather than
    as a worry. Growth is geometric at 1.05 per assessment until the cap binds.
    """
    arrears = Decimal("200.00")
    charged = []
    for _ in range(assessments):
        fee = late_fee_for(arrears)
        charged.append(fee)
        arrears += fee

    assert charged == sorted(charged), charged
    assert charged[-1] > charged[0]
    assert all(f <= LATE_FEE_FLAT for f in charged)
