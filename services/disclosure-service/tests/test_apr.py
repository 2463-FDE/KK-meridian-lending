"""APR money-math tests, with an oracle that does not come from apr.py.

The previous version of this file had one vector, and its "reference" value was
computed by a local function that re-implemented apr.py's own formula in
Decimal. It could catch a float-precision regression and nothing else. The
Week 1-4 client review named exactly this: *"your one test vector's expected
value mirrors the implementation, so it cannot catch a wrong implementation."*

It was right, and the cost was concrete. Under the mirrored reference,
`compute_apr(18000, 7.99, 48)` returning **5.196%** looked correct. The
actuarial answer is **9.584%** -- a 4.39 percentage-point understatement, 35x
the Reg Z tolerance, shipping on real disclosures. See apr.py's docstring.

Three sources of truth are used here, none of them apr.py:

1. **A structurally different algorithm.** `_oracle_apr` solves for the rate
   from the payment stream by Newton-Raphson with a numeric derivative; apr.py
   bisects. Different method, different failure modes -- they agree only if the
   underlying answer is right.

2. **A mathematical identity needing no implementation at all.** With no
   prepaid finance charge, the actuarial APR of a loan *must* equal its note
   rate, because the payment stream is priced at that rate by construction. Any
   implementation failing this is wrong, and nothing has to be looked up to
   know it. The old add-on formula fails it badly.

3. **Hand-checkable arithmetic.** Amount financed, total of payments and the
   finance-charge identity are single-step calculations stated inline, so a
   reader can verify them without running anything.

The vector set spans short and long terms, small and large principals, and a
zero-rate case -- the review's "more than one vector".
"""
from decimal import Decimal, getcontext

import pytest

from app import apr, fees, offer

getcontext().prec = 28

# Reg Z tolerance for a regular transaction: 1/8 of 1 percentage point.
TILA_APR_TOLERANCE = Decimal("0.125")

# (principal, note rate %, term months)
VECTORS = [
    (18000, "7.99", 48),    # the repo's original vector
    (5000, "5.00", 12),     # short term
    (50000, "12.50", 60),   # program maximum, long term
    (1200, "24.99", 6),     # small principal, high rate, very short
    (25000, "0.00", 36),    # promotional 0% -- the fee is the whole finance charge
    (9000, "7.99", 24),     # the seeded demo loan
]


def _oracle_apr(principal, note_rate_pct, term_months) -> Decimal:
    """Actuarial APR solved by Newton-Raphson, independent of apr.py.

    Deliberately a different numerical method from apr.py's bisection, so the
    two agreeing is evidence about the answer rather than about shared code.
    The payment and the amount financed are recomputed here from first
    principles for the same reason.
    """
    p = Decimal(str(principal))
    rate = Decimal(str(note_rate_pct))
    n = term_months

    # Level payment on the full principal at the note rate.
    r = rate / 100 / 12
    pmt = p / n if r == 0 else p * r * (1 + r) ** n / ((1 + r) ** n - 1)

    # The borrower receives principal less the prepaid finance charge.
    af = p - p * Decimal(str(fees.ORIGINATION_FEE_PCT))

    if pmt * n <= af:
        return Decimal(0)

    def pv(i: Decimal) -> Decimal:
        if i == 0:
            return pmt * n
        return pmt * (1 - (1 + i) ** -n) / i

    i = Decimal("0.01")
    h = Decimal("0.0000001")
    for _ in range(100):
        f = pv(i) - af
        if abs(f) < Decimal("0.0000000001"):
            break
        slope = (pv(i + h) - pv(i - h)) / (2 * h)
        i = i - f / slope
        if i <= 0:
            i = h
    return i * 12 * 100


# --- 1. the oracle agrees with the implementation, on every vector ------------

@pytest.mark.parametrize("principal,rate,term", VECTORS)
def test_disclosed_apr_matches_an_independently_solved_actuarial_apr(principal, rate, term):
    disclosed = Decimal(str(apr.compute_apr(principal, rate, term)))
    expected = _oracle_apr(principal, rate, term)
    assert abs(disclosed - expected) <= TILA_APR_TOLERANCE, (
        f"{principal} at {rate}% over {term}mo: disclosed {disclosed}, "
        f"independently solved {expected:.4f} -- outside the Reg Z tolerance"
    )


# --- 2. the identity that needs no reference implementation -------------------

@pytest.mark.parametrize("rate", ["7.99", "5.00", "12.50", "24.99"])
@pytest.mark.parametrize("term", [12, 36, 48, 60])
def test_with_no_prepaid_fee_the_apr_is_exactly_the_note_rate(monkeypatch, rate, term):
    """A loan with no prepaid finance charge is priced at its note rate by
    construction, so its actuarial APR must equal that rate exactly. True for
    mathematical reasons, not because an implementation says so -- which is
    what the old mirrored reference could never check. The add-on formula
    returned 5.04% for a no-fee 7.99% loan."""
    monkeypatch.setattr(fees, "ORIGINATION_FEE_PCT", Decimal("0"))
    monkeypatch.setattr(apr, "ORIGINATION_FEE_PCT", Decimal("0"))

    disclosed = Decimal(str(apr.compute_apr(18000, rate, term)))
    assert abs(disclosed - Decimal(rate)) <= Decimal("0.001"), (
        f"no-fee {rate}% loan over {term}mo disclosed {disclosed}, must be {rate}"
    )


def test_a_fee_can_only_push_the_apr_above_the_note_rate():
    """Direction check. A prepaid fee means the borrower receives less than the
    principal the payments are calculated on, so the APR must exceed the note
    rate -- never equal it, never fall below. The old formula got the direction
    wrong too, disclosing 5.196% on a 7.99% note."""
    for principal, rate, term in VECTORS:
        if Decimal(rate) == 0:
            continue
        disclosed = Decimal(str(apr.compute_apr(principal, rate, term)))
        assert disclosed > Decimal(rate), (
            f"{principal} at {rate}% over {term}mo disclosed {disclosed} -- "
            f"a prepaid fee cannot produce an APR at or below the note rate"
        )


def test_the_regression_case_by_name():
    """The exact vector and numbers from the finding, pinned so a revert is
    unmistakable rather than showing up as a vague tolerance drift."""
    assert apr.compute_apr(18000, 7.99, 48) == pytest.approx(9.584, abs=0.001)
    # What the add-on ratio used to return.
    assert apr.compute_apr(18000, 7.99, 48) != pytest.approx(5.196, abs=0.001)


# --- 3. hand-checkable arithmetic: the box has to foot ------------------------

@pytest.mark.parametrize("principal,rate,term", VECTORS)
def test_the_tila_box_foots(principal, rate, term):
    """amount financed + finance charge == total of payments.

    The identity every TILA box must satisfy. It used to fail by exactly the
    origination fee: `finance_charge()` returned interest only, leaving the
    prepaid fee out of the finance charge while still deducting it from the
    amount financed.
    """
    box = offer.build_offer(principal, float(rate), term)
    left = Decimal(str(box["amount_financed"])) + Decimal(str(box["finance_charge"]))
    right = Decimal(str(box["total_of_payments"]))
    assert abs(left - right) <= Decimal("0.01"), (
        f"{principal} at {rate}% over {term}mo: amount financed "
        f"{box['amount_financed']} + finance charge {box['finance_charge']} "
        f"= {left}, but total of payments is {right}"
    )


def test_amount_financed_is_principal_less_the_three_percent_fee():
    """Single-step arithmetic, checkable by eye: 3% of 18,000 is 540."""
    box = offer.build_offer(18000, 7.99, 48)
    assert box["amount_financed"] == pytest.approx(17460.00, abs=0.01)


def test_finance_charge_includes_the_prepaid_fee():
    """The disclosed finance charge must be interest plus the fee, not interest
    alone."""
    box = offer.build_offer(18000, 7.99, 48)
    interest_only = apr.monthly_payment(18000, 7.99, 48) * 48 - 18000
    assert box["finance_charge"] == pytest.approx(interest_only + 540.00, abs=0.02)
    assert box["finance_charge"] > interest_only


def test_a_zero_rate_loan_still_has_a_finance_charge_and_an_apr():
    """Promotional 0%: the fee is the entire cost of credit, so the APR is not
    zero. An implementation that echoed the note rate would report 0.00% here
    and understate the loan completely."""
    box = offer.build_offer(25000, 0.0, 36)
    assert box["finance_charge"] == pytest.approx(750.00, abs=0.01)   # 3% of 25,000
    assert apr.compute_apr(25000, 0.0, 36) > 1.0


# --- carried over from the original file --------------------------------------

def test_fee_constants_agree_across_modules():
    """The original D6 regression guard: one source of truth for the fee."""
    assert fees.ORIGINATION_FEE_PCT == apr.ORIGINATION_FEE_PCT == offer.ORIGINATION_FEE_PCT
