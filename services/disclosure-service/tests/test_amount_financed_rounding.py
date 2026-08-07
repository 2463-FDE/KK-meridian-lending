"""The amount financed must be a cent value before anything is priced from it.

Reviewed finding: `amount_financed_decimal` returned a full-precision Decimal.
For a principal of 1,002.50 the fee is 30.075 and the amount financed 972.425 --
and the APR, the finance charge and the total were all solved against that
fraction of a cent. `offers.amount_financed` is NUMERIC(14,2), so the figure
actually stored and disclosed is 972.43. Every derived number was therefore
priced against an amount financed that exists nowhere, and the TILA box could
not foot against its own stored values.

Two things are asserted, and they are different claims:

  1. the amount financed is exactly 972.43 -- the rounding direction and the
     order of operations both matter here, see below;
  2. the disclosure FOOTS: amount financed + finance charge = total of
     payments, to the cent, using the values that would be persisted.

1,002.50 is not an arbitrary vector. It is a half-cent boundary on the fee
(30.075) AND on the amount financed (972.425), so it distinguishes three
implementations that agree everywhere else:

    round half-up on the difference   -> 972.43   (correct, matches Postgres)
    round half-to-even on the difference -> 972.42 (Python's round())
    round the fee first, then subtract   -> 972.42 (30.08 subtracted)

The third is the subtle one: it is not a rounding-mode error at all, it is an
order-of-operations error, and it produces the same wrong answer. db/init and
db/tools/regenerate_seed_offers.py both round `principal - principal * fee_pct`,
so this is also what keeps the application and the seed data consistent.
"""
from decimal import Decimal

import pytest

from app import apr, fees, offer

PRINCIPAL = 1002.50
NOTE_RATE = 7.99
TERM = 36

EXPECTED_AMOUNT_FINANCED = Decimal("972.43")
EXPECTED_PREPAID_FEE = Decimal("30.07")


def test_the_regression_vector_amount_financed_is_972_43():
    """The exact figure from the finding."""
    assert apr.amount_financed_decimal(PRINCIPAL) == EXPECTED_AMOUNT_FINANCED


def test_the_amount_financed_is_always_a_cent_value():
    """Not merely correct for this vector -- quantized as a property.

    A Decimal with more than two places reaching the APR solver is the defect,
    whatever its value.
    """
    for principal in (PRINCIPAL, 1000, 9000, 15000, 18000, 1234.56, 49999.99):
        af = apr.amount_financed_decimal(principal)
        assert -af.as_tuple().exponent <= 2, f"{principal}: {af} is not a cent value"


def test_the_fee_and_the_amount_financed_sum_to_the_principal_exactly():
    """The property that forces the order of operations.

    Rounding the fee independently would give 30.08 and 972.42, which sum to
    1,002.50 as well -- but disagree with what the database stores. Deriving the
    fee as principal - amount_financed keeps both identities true at once.
    """
    af = apr.amount_financed_decimal(PRINCIPAL)
    fee = apr.prepaid_finance_charge_decimal(PRINCIPAL)
    assert fee == EXPECTED_PREPAID_FEE
    assert af + fee == Decimal(str(PRINCIPAL))


def test_the_tila_box_foots_to_the_cent_for_the_regression_vector():
    """The consequence the finding named: amount financed + finance charge must
    equal the total of payments, in the values that get persisted."""
    built = offer.build_offer(PRINCIPAL, NOTE_RATE, TERM)

    af = Decimal(str(built["amount_financed"]))
    fc = Decimal(str(built["finance_charge"]))
    total = Decimal(str(built["total_of_payments"]))

    assert af == EXPECTED_AMOUNT_FINANCED
    assert af + fc == total, (
        f"box does not foot: {af} + {fc} = {af + fc}, total says {total}"
    )


def test_the_total_is_the_sum_of_the_actual_model_b_payments():
    """And the total itself is not a multiplication.

    Under Model B the box footing is only meaningful if the total is the sum of
    the payments actually billed -- otherwise both sides could agree on a figure
    the borrower never pays.
    """
    built = offer.build_offer(PRINCIPAL, NOTE_RATE, TERM)
    summed = (
        Decimal(str(built["monthly_payment"])) * built["regular_payment_count"]
        + Decimal(str(built["final_payment"]))
    )
    assert summed == Decimal(str(built["total_of_payments"]))


def test_every_disclosed_amount_is_a_cent_value():
    """A fraction of a cent anywhere in the box is a figure that cannot be
    stored, and therefore cannot be disclosed."""
    built = offer.build_offer(PRINCIPAL, NOTE_RATE, TERM)
    for field in ("finance_charge", "monthly_payment", "final_payment",
                  "amount_financed", "total_of_payments"):
        value = Decimal(str(built[field]))
        assert -value.as_tuple().exponent <= 2, f"{field} = {value} is not a cent value"


# A 3% fee lands on a half cent exactly when the principal ends in .50:
# 1002.50 x 0.03 = 30.075. 1005.00 gives 30.15 and is not a boundary at all --
# it was in an earlier version of this list, and the assertion below caught it,
# which is the point of asserting the premise rather than assuming it.
@pytest.mark.parametrize("principal", [1002.50, 1005.50, 100.50, 3337.50])
def test_half_cent_fee_boundaries_all_round_half_up(principal):
    """A family of principals whose fee lands exactly on a half cent.

    Each one distinguishes half-up from half-to-even, so a future change of
    rounding mode fails here rather than only on the single vector above.
    """
    p = Decimal(str(principal))
    exact = p - p * Decimal(str(fees.ORIGINATION_FEE_PCT))
    # Only meaningful if this really is a boundary case.
    assert exact != exact.quantize(Decimal("0.01")), f"{principal} is not a half-cent case"
    expected = exact.quantize(Decimal("0.01"), rounding="ROUND_HALF_UP")
    assert apr.amount_financed_decimal(principal) == expected
