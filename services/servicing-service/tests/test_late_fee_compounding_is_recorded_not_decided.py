"""Compounding is real, the question is now ANSWERED, and the behaviour is unchanged.

`docs/DEBT.md` D23. The client asked at the 2026-08-19 demo whether the late fee
is meant to compound. It does: the fee is the lesser of $35 and five per cent of
arrears, a posted fee raises arrears, so the next assessment prices off a base
that already contains the previous fee.

**The answer arrived on 2026-08-29, and it replaced the rule rather than
settling a cadence:** at most one fee per missed scheduled installment, after
the existing grace period, priced at `min($35, 5% x unpaid scheduled PRINCIPAL +
INTEREST for THAT installment)`, with previous late fees and all other fees
excluded from the base.

**These tests still pass, and that is correct, because the BEHAVIOUR has not
changed.** PR #143 landed fee-installment identity and the database guarantee
that one cited installment can receive at most one fee. The money path does not
use that primitive: nothing records which installment a payment satisfied, so
unpaid scheduled P&I after a payment cannot be derived without choosing an
allocation order. The exact grace-period boundary and that allocation order are
not present in the current authority artifacts. Until both are supplied, the
runtime remains on `past_due` rather than approximating the decided rule. D23
separates the landed primitive from that uncut runtime path.

So what these are has shifted, and saying so is the point. They were
characterization tests of an UNDECIDED rule; they are now characterization tests
of a SUPERSEDED one. They pin what the system really does so that the register
entry cannot quietly go stale, and so that whoever implements the decided rule
can see exactly what changes. When that lands, these tests are expected to fail
-- loudly -- rather than leaving two accounts of the fee in the repository.

Deliberately arithmetic-only: no database, no ledger. The concurrency half of
this area is closed and proved elsewhere with a real two-connection race
(`test_late_fee_goes_through_the_ledger.py`). Mixing the two would blur a closed
engineering defect with a product rule that is decided but unbuilt, which is the
confusion D23 exists to prevent.
"""
import pathlib
from decimal import Decimal

import pytest

from app.delinquency import LATE_FEE_FLAT, LATE_FEE_PCT_OF_PAST_DUE, late_fee_for

REPO = pathlib.Path(__file__).resolve().parents[3]
FEE_SCHEDULE = REPO / "policies" / "fee_schedule.md"


def test_the_schedule_publishes_the_decided_rule_and_says_the_code_differs():
    """The premise, and it changed on 2026-08-29 exactly as this file predicted.

    This test used to assert that `policies/fee_schedule.md` still published
    "$35 flat, or 5% of the past-due amount, whichever is **less**". It does not
    any more: the client decided the rule, and the published table now states
    the decided one -- one fee per missed scheduled installment, priced off that
    installment's unpaid scheduled principal and interest. The old assertion
    failed the moment the table was updated, which is what it was for.

    That matters beyond bookkeeping. `policies/fee_schedule.md` is on policy
    chat's allowlist (`loan-assistant/app/policy_tool.py`), so whatever this
    file publishes is what a client-facing answer can quote as current policy.
    Leaving the superseded arrears rule under a "source of truth" heading would
    have had the assistant state an obsolete fee rule as fact.

    So the premise is now two-sided, and BOTH sides have to hold:

      * the schedule publishes the DECIDED rule; and
      * the schedule says plainly that the implementation differs.

    The second is what keeps the tests below honest. They characterize the
    OLDER comparison, which is still what the code computes, and a reader who
    finds them without that sentence would reasonably think the repository had
    two contradictory accounts of the fee.
    """
    text = FEE_SCHEDULE.read_text(encoding="utf-8")

    # The decided rule is what the published table states.
    assert "one fee per missed scheduled installment" in text.lower()
    assert "unpaid scheduled principal + interest" in text.lower()

    # And the gap is stated rather than left for a reader to infer.
    assert "Current implementation differs" in text

    # The constants are unchanged, because the BEHAVIOUR is unchanged: these are
    # the older published comparison, which is still what runs.
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
        "holds, the decided rule in DEBT.md D23 has reached the code, and this "
        "file describes a fee that no longer exists"
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
