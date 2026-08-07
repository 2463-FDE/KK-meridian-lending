"""APR money-math tests.

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

# The zero-fee identity is exact in algebra but not in floating arithmetic: the
# solver converges to a tolerance and compute_apr rounds to 3 decimals for
# display. 0.001 is that display granularity -- one unit in the last disclosed
# place -- so the assertion stays tight without being brittle. Deliberately NOT
# exact equality, and deliberately far tighter than the 0.125 Reg Z tolerance,
# which on this particular check would let a real defect through.
ZERO_FEE_APR_TOLERANCE = Decimal("0.001")

# (principal, note rate %, term months)
VECTORS = [
    (18000, "7.99", 48),    # the repo's original vector
    (5000, "5.00", 12),     # short term
    (50000, "12.50", 60),   # program maximum, long term
    (1200, "24.99", 6),     # small principal, high rate, very short
    (25000, "0.00", 36),    # promotional 0% -- the fee is the whole finance charge
    (9000, "7.99", 24),     # the seeded demo loan
    (15000, "7.99", 36),    # RV-2: second reproduction of the finding, see below
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


# --- 1. a second, structurally different solver agrees ------------------------
#     Corroboration, NOT an outside oracle: both implementations live in this
#     repository. The outside oracle is the FFIEC vector in section 4.

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
def test_with_no_prepaid_fee_the_apr_equals_the_note_rate_within_tolerance(
    monkeypatch, rate, term
):
    """A loan with no prepaid finance charge is priced at its note rate by
    construction, so its actuarial APR must equal that rate. True for
    mathematical reasons, not because an implementation says so -- which is what
    the old mirrored reference could never check. The add-on formula returned
    5.04% for a no-fee 7.99% loan.

    Compared within ZERO_FEE_APR_TOLERANCE rather than by exact equality: the
    identity is exact in algebra, but the solver converges to a tolerance and the
    result is rounded to 3 decimals for display.
    """
    monkeypatch.setattr(fees, "ORIGINATION_FEE_PCT", Decimal("0"))
    monkeypatch.setattr(apr, "ORIGINATION_FEE_PCT", Decimal("0"))

    disclosed = Decimal(str(apr.compute_apr(18000, rate, term)))
    delta = abs(disclosed - Decimal(rate))
    assert delta <= ZERO_FEE_APR_TOLERANCE, (
        f"no-fee {rate}% loan over {term}mo disclosed {disclosed} "
        f"(off by {delta}, tolerance {ZERO_FEE_APR_TOLERANCE})"
    )


@pytest.mark.parametrize("principal,rate,term", [v for v in VECTORS if Decimal(v[1]) != 0])
def test_a_positive_prepaid_fee_raises_the_apr_with_payments_unchanged(
    monkeypatch, principal, rate, term
):
    """Monotonicity, scoped to exactly one variable.

    A positive prepaid finance charge reduces the amount financed while leaving
    the payment stream untouched -- the borrower receives less for the same
    payments -- so the APR must strictly increase. Toggling the fee on the same
    loan isolates that. Comparing an APR against a note rate across differing
    vectors does not, because principal, rate and term all move at once.

    The unchanged-cash-flows premise is asserted rather than assumed: if some
    future change made the fee alter the payment, this test would otherwise
    silently begin measuring something else.
    """
    monkeypatch.setattr(fees, "ORIGINATION_FEE_PCT", Decimal("0"))
    monkeypatch.setattr(apr, "ORIGINATION_FEE_PCT", Decimal("0"))
    payment_without_fee = apr.monthly_payment(principal, rate, term)
    apr_without_fee = Decimal(str(apr.compute_apr(principal, rate, term)))

    monkeypatch.setattr(fees, "ORIGINATION_FEE_PCT", Decimal("0.030"))
    monkeypatch.setattr(apr, "ORIGINATION_FEE_PCT", Decimal("0.030"))
    payment_with_fee = apr.monthly_payment(principal, rate, term)
    apr_with_fee = Decimal(str(apr.compute_apr(principal, rate, term)))

    assert payment_with_fee == pytest.approx(payment_without_fee, abs=1e-9), (
        "premise broken: the prepaid fee changed the payment stream, so this test "
        "is no longer isolating the effect of the fee"
    )
    assert apr_with_fee > apr_without_fee, (
        f"{principal} at {rate}% over {term}mo: APR {apr_with_fee} with a 3% "
        f"prepaid fee is not above {apr_without_fee} without one"
    )
    # The borrower-facing form of the same statement.
    assert apr_with_fee > Decimal(rate)


def test_the_second_reported_vector_by_name():
    """RV-2 -- reproduced independently from the running UI, 2026-08-07.

    Reported disclosure for 15,000 at a 7.99% note rate over 36 months with the
    3% origination fee:

        APR                5.43        <- wrong; and note 5.43 < 7.99, impossible
        finance charge     1,919.15    <- interest only, fee omitted
        amount financed   14,550.00
        total of payments 16,919.15

    The box did not foot: 14,550.00 + 1,919.15 = 16,469.15, short of the stated
    16,919.15 by 450.00 -- exactly the origination fee. Two separate defects
    showing in one disclosure: the APR came from the add-on ratio, and the
    finance charge left the prepaid fee out.

    Correct values, each recomputed here from the payment stream:
        monthly payment   469.976287...  (469.98 displayed)
        total of payments 16,919.15      (unrounded payment x 36, then rounded)
        finance charge     2,369.15      (total - amount financed)
        actuarial APR     10.07151977...  -> 10.072 displayed
    """
    box = offer.build_offer(15000, 7.99, 36)

    # 2. the APR is the actuarial rate, not the add-on ratio
    assert apr.compute_apr(15000, 7.99, 36) == pytest.approx(10.072, abs=0.001)
    assert apr.compute_apr(15000, 7.99, 36) != pytest.approx(5.43, abs=0.01)
    # 1./2. and it can never sit below the note rate it was priced at
    assert apr.compute_apr(15000, 7.99, 36) > 7.99

    # 3. finance charge includes the prepaid fee
    assert box["finance_charge"] == pytest.approx(2369.15, abs=0.01)
    assert box["finance_charge"] != pytest.approx(1919.15, abs=0.01)

    # 4. amount financed is principal less the 3% fee
    assert box["amount_financed"] == pytest.approx(14550.00, abs=0.01)

    # 5. total of payments
    assert box["total_of_payments"] == pytest.approx(16919.15, abs=0.01)

    # 6. the box foots -- the identity the reported disclosure failed
    assert box["amount_financed"] + box["finance_charge"] == pytest.approx(
        box["total_of_payments"], abs=0.01
    ), "amount financed + finance charge must equal total of payments"

    # the disclosed monthly payment servicing has to reproduce
    assert box["monthly_payment"] == pytest.approx(469.98, abs=0.01)


def test_the_regression_case_by_name():
    """The exact vector and numbers from the finding, pinned so a revert is
    unmistakable rather than showing up as a vague tolerance drift."""
    assert apr.compute_apr(18000, 7.99, 48) == pytest.approx(9.584, abs=0.001)
    # What the add-on ratio used to return.
    assert apr.compute_apr(18000, 7.99, 48) != pytest.approx(5.196, abs=0.001)


# --- 4. the OUTSIDE oracle: a federally-published tool -----------------------
#     Sections 1-3 are corroboration only -- every one of them lives in this
#     repository, so collectively they can prove the implementation is
#     self-consistent but never that it agrees with the regulator's own
#     arithmetic. This section is the only outside evidence.

# Filled in from a manual run of the FFIEC APR tool. Every field is required:
# a vector with no recorded provenance is not outside evidence, it is a number
# somebody typed.
FFIEC_VECTOR = {
    "tool": None,              # e.g. "FFIEC APR Computational Tool", incl. version
    "url": None,               # where it was obtained
    "amount_financed": Decimal("17460.00"),
    "payment": Decimal("439.35"),
    "payments": 48,
    "frequency": "monthly",
    "first_period": "regular",
    "balloon": None,           # none
    "expected_apr": None,      # <-- the APR the tool displayed
    "verified_on": None,       # date of the run
    "tolerance": TILA_APR_TOLERANCE,   # 12 CFR 1026.22(a)(1), regular transaction
}


@pytest.mark.skipif(
    FFIEC_VECTOR["expected_apr"] is None,
    reason=(
        "FFIEC APR tool result not yet captured. This is the only OUTSIDE oracle in "
        "this file and PR #10 must not merge while it is skipped -- sections 1-3 are "
        "all internal to this repository. To close: run the FFIEC APR tool with "
        "amount financed 17,460.00 / 48 monthly payments / 439.35 / regular first "
        "period / no balloon, then fill in tool, url, expected_apr and verified_on."
    ),
)
def test_the_apr_matches_a_federally_published_tool():
    """The disclosed APR against a regulator-published computation of the same
    payment stream -- the one check in this file that does not depend on any code
    in this repository being correct.

    Compared within the 12 CFR 1026.22(a)(1) tolerance for a regular transaction
    (0.125 percentage points), which is the legal standard rather than an
    arbitrary epsilon.
    """
    v = FFIEC_VECTOR
    assert v["tool"] and v["url"] and v["verified_on"], (
        "an FFIEC vector without tool, url and date is not outside evidence"
    )
    # Recover the note rate that produces this payment on the gross principal,
    # then disclose through the same path production uses.
    principal = v["amount_financed"] / (1 - fees.ORIGINATION_FEE_PCT)
    note_rate = Decimal(str(apr.note_rate_from_payment(
        float(principal), float(v["payment"]), v["payments"]
    )))
    disclosed = Decimal(str(apr.compute_apr(float(principal), float(note_rate), v["payments"])))

    delta = abs(disclosed - v["expected_apr"])
    assert delta <= v["tolerance"], (
        f"disclosed {disclosed} vs {v['tool']} {v['expected_apr']} "
        f"(delta {delta}, permitted {v['tolerance']}) -- outside the Reg Z tolerance"
    )


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
