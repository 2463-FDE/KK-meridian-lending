"""Where the difference went, and why the server works it out rather than the UI.

"Amount Financed -- the amount of credit provided to you" is a NET figure. A
borrower who applied for $9,000 and reads $8,730 is looking at a $270 gap with
nothing on the page to explain it, because the origination fee is prepaid and
never reaches them.

Two things decide whether that breakdown can be shown honestly, and both are
tested here rather than in the browser.

**The fee is a SUBTRACTION, not the percentage applied again.**
`apr.amount_financed_decimal` stores `ROUND_HALF_UP(principal - principal *
fee_pct)` -- the difference is rounded, deliberately, so the TILA box foots
against its own stored values. Recomputing the fee as `round(principal *
fee_pct)` therefore disagrees with the stored figures by a cent on exactly the
inputs the rounding exists for: a $1,002.50 principal stores $972.43, whose
difference is $30.07, while `round(1002.50 * 0.03)` is $30.08. On screen that is
three numbers that do not add up, in a federal disclosure box.

**A legacy row cannot show one at all.** Pre-0030 offers stored no principal,
and the only way to produce one is to invert the amount financed through the fee
-- which lands on a NEIGHBOURING principal, because the amount financed is
cent-rounded. That inversion is legitimate for redrawing a schedule, where it is
labelled a reconstruction. Printed under "the amount you asked for" it is a
contractual figure the borrower was never quoted, sitting beside genuine
disclosed amounts with nothing to distinguish it.
"""
from decimal import Decimal

import pytest

from app import apr, fees
from app.routers.offers import _amount_financed_breakdown


# --- the arithmetic, and why it is a subtraction -------------------------------


@pytest.mark.parametrize("principal", ["9000.00", "1002.50", "12000.00", "49999.99",
                                       "15000.00", "5000.00"])
def test_the_breakdown_always_foots(principal):
    """The property that matters on screen: the three numbers add up, exactly,
    for every principal -- including the ones whose fee lands on a half cent."""
    stored_principal = Decimal(principal)
    stored_af = apr.amount_financed_decimal(stored_principal)

    requested, fee = _amount_financed_breakdown(stored_principal, stored_af)

    assert Decimal(str(requested)) - Decimal(str(fee)) == stored_af, (
        f"requested {requested} less fee {fee} does not equal the stored amount "
        f"financed {stored_af}")


def test_the_fee_is_the_difference_not_the_percentage_reapplied():
    """The specific input the rounding rule exists for.

    $1,002.50 at 3% is a fee of $30.075. The stored amount financed rounds the
    DIFFERENCE to $972.43, so the fee the borrower actually paid is $30.07.
    Rounding the FEE instead gives $30.08 -- and a box that says 1002.50 minus
    30.08 is 972.43.
    """
    principal = Decimal("1002.50")
    stored_af = apr.amount_financed_decimal(principal)
    assert stored_af == Decimal("972.43")

    _, fee = _amount_financed_breakdown(principal, stored_af)

    assert Decimal(str(fee)) == Decimal("30.07")
    reapplied = (principal * fees.ORIGINATION_FEE_PCT).quantize(Decimal("0.01"))
    assert reapplied == Decimal("30.08"), (
        "this test's premise is gone -- the percentage no longer disagrees with "
        "the difference on this input, so it proves nothing")
    assert Decimal(str(fee)) != reapplied


def test_a_zero_fee_offer_still_has_a_breakdown():
    """`0.00` is a real disclosure, not a missing one. A truthiness check
    anywhere on this path would turn a zero-fee offer into "breakdown
    unavailable"."""
    requested, fee = _amount_financed_breakdown(Decimal("5000.00"), Decimal("5000.00"))

    assert requested == 5000.00
    assert fee == 0.00
    assert fee is not None


# --- when it must refuse -------------------------------------------------------


def test_a_legacy_row_with_no_stored_principal_reports_no_breakdown():
    """The pre-0030 case. Null, not an inverted principal."""
    assert _amount_financed_breakdown(None, Decimal("8730.00")) == (None, None)


def test_a_row_with_no_amount_financed_reports_no_breakdown():
    assert _amount_financed_breakdown(Decimal("9000.00"), None) == (None, None)


def test_stored_figures_that_imply_a_negative_fee_report_nothing(caplog):
    """An amount financed larger than the principal is a real inconsistency in a
    signed disclosure. It is logged and refused rather than rendered: "less
    origination fee -$50.00" on a borrower's screen is worse than an absent
    breakdown, and it would be a claim nobody can support."""
    with caplog.at_level("ERROR"):
        assert _amount_financed_breakdown(Decimal("9000.00"),
                                          Decimal("9050.00")) == (None, None)

    assert any("exceeds stored principal" in r.getMessage()
               for r in caplog.records), "the inconsistency was not reported"


def test_the_fee_never_comes_from_the_live_fee_constant():
    """A fee-policy change must not move a figure on an existing offer.

    Asserted by construction rather than by mocking: the helper takes two stored
    amounts and no percentage, so there is nothing for a policy change to reach.
    This test is what fails if a future version reintroduces one.
    """
    import ast
    import inspect

    # The CODE, with the docstring removed. The first version of this test read
    # the whole source and failed on its own explanation of why the fee is not
    # the percentage -- a guard that cannot survive the comment describing it is
    # matching prose, not behaviour.
    tree = ast.parse(inspect.getsource(_amount_financed_breakdown).lstrip())
    fn = tree.body[0]
    if (fn.body and isinstance(fn.body[0], ast.Expr)
            and isinstance(fn.body[0].value, ast.Constant)
            and isinstance(fn.body[0].value.value, str)):
        fn.body = fn.body[1:]
    source = ast.unparse(fn)

    for forbidden in ("ORIGINATION_FEE_PCT", "fee_pct", "LEGACY_PRE_SNAPSHOT"):
        assert forbidden not in source, (
            f"the breakdown reads {forbidden}. The fee a borrower paid is the "
            f"difference between two stored amounts; reading a rate at display "
            f"time is how a policy change silently restates an existing offer, "
            f"which is the drift `fee_pct_used` was added to prevent")
