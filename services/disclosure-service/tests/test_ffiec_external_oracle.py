"""The outside oracle: an APR this repository did not calculate.

Everything in `test_apr.py` sections 1-3 is corroboration. A second solver, a
closed-form identity and a golden schedule all live in this repository, so
together they can prove the implementation is self-consistent and never that it
agrees with the regulator's own arithmetic. This file is the only evidence of
the second kind, and it is deliberately in a file of its own so it cannot be
mistaken for one of the internal checks.

The expected APR below is TYPED IN from an external run. Nothing in this file
may derive it -- not `apr.py`, not `schedule.py`, not the Newton-Raphson helper,
not the second solver in test_apr.py. If a future edit computes this number, the
test stops being outside evidence and becomes a tautology that passes whatever
the code happens to do.
"""
from decimal import Decimal

from app import offer as offer_mod

# --- the external result, transcribed -----------------------------------------
#
# Tool:            FFIEC APR Computational Tool (official, federally published)
# URL:             https://www.ffiec.gov/aprwin.htm
# Verified on:     2026-08-09
# Evidence:        https://github.com/2463-FDE/KK-meridian-lending/pull/10#issuecomment-5234867571
#                  (the tool's output PDF is attached to that comment; it is
#                  deliberately NOT committed here -- a binary in the tree is
#                  not more trustworthy than the link, and the PR comment is the
#                  record of who ran it and when)
#
# Inputs entered, exactly as the loan discloses them:
#   loan type          regular unsecured installment loan
#   amount financed    $14,550.00
#   frequency          monthly
#   stream 1           35 payments of $469.98, beginning at unit period 1
#   stream 2           1 payment of $469.87, at unit period 36
#   odd days           0
#   finance charge     $2,369.17
#   total of payments  $16,919.17
#
# The two streams are entered SEPARATELY. Entering 36 level payments of $469.98
# would describe a cash flow this system does not produce -- the final period
# bills remaining principal plus that period's interest -- and the tool would
# then return a correct APR for the wrong loan, which would look exactly like
# verification.
FFIEC_EXPECTED_APR = Decimal("10.0717")

FFIEC_AMOUNT_FINANCED = Decimal("14550.00")
FFIEC_FINANCE_CHARGE = Decimal("2369.17")
FFIEC_TOTAL_OF_PAYMENTS = Decimal("16919.17")
FFIEC_REGULAR_PAYMENT = Decimal("469.98")
FFIEC_REGULAR_PAYMENT_COUNT = 35
FFIEC_FINAL_PAYMENT = Decimal("469.87")
FFIEC_TERM_MONTHS = 36

# Meridian displays the APR to three decimals, so the most it can differ from an
# exact four-decimal figure is half of one unit in its last displayed place.
# 10.0717 rounds to 10.072, and 0.0005 is that display granularity -- this bound
# is about ROUNDING, not about tolerance.
#
# Stated separately, because conflating the two is how a real breach gets waved
# through: the applicable regulatory tolerance for a regular transaction is
# 0.125 percentage points (12 CFR 1026.22(a)(1)), which is 250 times looser than
# what is asserted here. Passing this test says the disclosure is arithmetically
# right, not merely legally permissible.
DISPLAY_ROUNDING_BOUND = Decimal("0.0005")

# The application that produces this cash flow: $15,000 principal, 3% prepaid
# origination fee, 7.99% note rate, 36 months.
PRINCIPAL = 15000.0
NOTE_RATE_PCT = 7.99


def test_disclosed_apr_matches_the_independent_ffiec_vector():
    """Meridian's disclosed APR agrees with the FFIEC tool's own calculation.

    Tool:        FFIEC APR Computational Tool
    URL:         https://www.ffiec.gov/aprwin.htm
    Verified on: 2026-08-09
    Streams:     35 x $469.98 from unit period 1; 1 x $469.87 at unit period 36
                 (monthly, 0 odd days, amount financed $14,550.00)
    Evidence:    https://github.com/2463-FDE/KK-meridian-lending/pull/10#issuecomment-5234867571

    FFIEC calculated 10.0717%. Meridian discloses 10.072%. The difference is
    0.0003 percentage points, which is three-decimal display rounding.

    The offer is built through the PRODUCTION path (`offer.build_offer`), the
    same function the POST /offers handler calls, so this asserts what the
    service would actually disclose rather than what a test-local calculation
    thinks it should.
    """
    box = offer_mod.build_offer(PRINCIPAL, NOTE_RATE_PCT, FFIEC_TERM_MONTHS)

    # The cash flow the FFIEC tool was given. If any of these drift, the vector
    # above stops describing this loan and the APR comparison below is
    # meaningless -- so they are asserted first, and exactly.
    assert Decimal(str(box["amount_financed"])) == FFIEC_AMOUNT_FINANCED
    assert Decimal(str(box["finance_charge"])) == FFIEC_FINANCE_CHARGE
    assert Decimal(str(box["total_of_payments"])) == FFIEC_TOTAL_OF_PAYMENTS
    assert Decimal(str(box["monthly_payment"])) == FFIEC_REGULAR_PAYMENT
    assert box["regular_payment_count"] == FFIEC_REGULAR_PAYMENT_COUNT
    assert Decimal(str(box["final_payment"])) == FFIEC_FINAL_PAYMENT
    assert box["regular_payment_count"] + 1 == FFIEC_TERM_MONTHS

    # The streams must also sum to the total the tool was given. This is the
    # operator's cross-check while capturing the vector, kept here so a drifting
    # payment cannot pass by coincidence.
    stream_total = (
        FFIEC_REGULAR_PAYMENT * FFIEC_REGULAR_PAYMENT_COUNT + FFIEC_FINAL_PAYMENT
    )
    assert stream_total == FFIEC_TOTAL_OF_PAYMENTS

    # What the borrower is shown.
    disclosed = Decimal(str(box["apr"]))
    assert disclosed == Decimal("10.072")

    # The comparison this file exists for: our number against theirs.
    difference = abs(disclosed - FFIEC_EXPECTED_APR)
    assert difference <= DISPLAY_ROUNDING_BOUND, (
        f"disclosed APR {disclosed} differs from the FFIEC tool's "
        f"{FFIEC_EXPECTED_APR} by {difference} percentage points, more than the "
        f"{DISPLAY_ROUNDING_BOUND} attributable to three-decimal display "
        f"rounding. Re-run the FFIEC tool before changing this bound."
    )


def test_the_ffiec_expected_apr_is_not_derived_from_repository_code():
    """The property that makes the test above outside evidence.

    A vector computed by the code under test certifies nothing. This asserts the
    expected value is a literal and that this module imports no calculator --
    cheap to check, and the failure mode it guards against (someone "fixing" a
    drifting vector by computing it) is silent and total.
    """
    import ast
    import pathlib

    source = pathlib.Path(__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    # FFIEC_EXPECTED_APR must be assigned a plain literal.
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "FFIEC_EXPECTED_APR"
            for t in node.targets
        ):
            assert isinstance(node.value, ast.Call), "expected Decimal(\"...\")"
            assert isinstance(node.value.func, ast.Name) and node.value.func.id == "Decimal"
            assert len(node.value.args) == 1
            assert isinstance(node.value.args[0], ast.Constant), (
                "the FFIEC APR must be a typed-in constant, never computed"
            )
            break
    else:
        raise AssertionError("FFIEC_EXPECTED_APR is no longer assigned in this module")

    # None of the repository's calculators may be reachable from here.
    forbidden = {"apr", "schedule", "newton", "compute_apr", "apr_from_cash_flows"}
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name.split(".")[-1] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.update(a.name.split(".")[-1] for a in node.names)
            if node.module:
                imported.add(node.module.split(".")[-1])
    leaked = forbidden & imported
    assert not leaked, (
        f"this module imports {sorted(leaked)} -- the expected APR must come "
        f"from the external tool, not from the code being verified"
    )
