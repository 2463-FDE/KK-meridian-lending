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

from app import apr, fees, offer, schedule

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

    Correct values under the CONTRACTUAL cash-flow model (Model B), each
    recomputed here from the schedule the borrower actually receives:
        35 regular payments  469.98
        final payment        469.87   (absorbs the cent residue)
        total of payments 16,919.17   (SUM of actual payments)
        finance charge     2,369.17   (total - amount financed)
        APR               10.072      (solved from the actual sequence)

    Supersedes an earlier statement of 16,919.15 / 2,369.15. Those came from
    unrounded payment x term, which is not what anybody pays: the schedule's own
    payments summed to 16,919.17, so the disclosed total was 2 cents adrift from
    the disclosure it was attached to. Authorized correction, 2026-08-07.
    """
    box = offer.build_offer(15000, 7.99, 36)

    # 2. the APR is the actuarial rate, not the add-on ratio
    assert apr.compute_apr(15000, 7.99, 36) == pytest.approx(10.072, abs=0.001)
    assert apr.compute_apr(15000, 7.99, 36) != pytest.approx(5.43, abs=0.01)
    # 1./2. and it can never sit below the note rate it was priced at
    assert apr.compute_apr(15000, 7.99, 36) > 7.99

    # 3. finance charge includes the prepaid fee
    assert box["finance_charge"] == pytest.approx(2369.17, abs=0.01)
    assert box["finance_charge"] != pytest.approx(1919.15, abs=0.01)

    # 4. amount financed is principal less the 3% fee
    assert box["amount_financed"] == pytest.approx(14550.00, abs=0.01)

    # 5. total of payments
    assert box["total_of_payments"] == pytest.approx(16919.17, abs=0.01)

    # 6. the box foots -- the identity the reported disclosure failed
    assert box["amount_financed"] + box["finance_charge"] == pytest.approx(
        box["total_of_payments"], abs=0.01
    ), "amount financed + finance charge must equal total of payments"

    # the disclosed regular payment servicing has to reproduce, and the
    # adjusted final payment the disclosure must also present
    assert box["monthly_payment"] == pytest.approx(469.98, abs=0.01)
    assert box["final_payment"] == pytest.approx(469.87, abs=0.01)
    assert box["regular_payment_count"] == 35


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
# The vector describes the ACTUAL Model B cash flow, which is NOT 48 level
# payments. It is 47 payments of 439.35 and a final payment of 439.25 -- the
# final period bills remaining principal plus that period's interest.
#
# This mattered: the vector previously said "48 monthly payments of 439.35",
# and capturing that from the FFIEC tool would have certified an APR for a cash
# flow this system does not produce. The tool would have returned a correct
# answer to the wrong question, and the result would have looked like outside
# verification. The irregular final payment must be entered as its own payment
# stream.
#
# Cross-check while capturing: the sum of the entered payments must be
# 21,088.70 (= 47 x 439.35 + 439.25). If the tool's own total differs, the
# stream was entered wrongly and the APR it reports is not this loan's.
#
# Every provenance field is required. A vector with no recorded provenance is
# not outside evidence, it is a number somebody typed.
FFIEC_VECTOR = {
    "tool": None,              # e.g. "FFIEC APR Computational Tool", incl. version
    "url": None,               # where it was obtained
    "amount_financed": Decimal("17460.00"),
    # Two streams, in order. Entered separately in the tool, not averaged.
    "regular_payment": Decimal("439.35"),
    "regular_payment_count": 47,
    "final_payment": Decimal("439.25"),
    "total_of_payments": Decimal("21088.70"),   # operator cross-check
    "frequency": "monthly",
    "first_period": "regular",
    "balloon": None,           # none -- the final payment is an adjustment, not a balloon
    "expected_apr": None,      # <-- the APR the tool displayed
    "verified_on": None,       # date of the run
    "tolerance": TILA_APR_TOLERANCE,   # 12 CFR 1026.22(a)(1), regular transaction
}


def _ffiec_payment_sequence(v: dict) -> list:
    return [v["regular_payment"]] * v["regular_payment_count"] + [v["final_payment"]]


def test_the_ffiec_vector_describes_the_cash_flow_this_system_actually_discloses():
    """Guards the vector itself, and runs whether or not the capture has happened.

    An outside oracle is only evidence if it was asked about the right cash
    flow. This asserts the payment stream written above is the one the
    production generator produces for this loan -- so if Model B's rounding ever
    changes, the vector goes stale loudly instead of continuing to certify a
    superseded schedule.
    """
    v = FFIEC_VECTOR
    principal = v["amount_financed"] / (1 - Decimal(str(fees.ORIGINATION_FEE_PCT)))
    rows = schedule.amortization(
        float(principal), fees.NOTE_RATE_PCT, v["regular_payment_count"] + 1
    )
    produced = [Decimal(str(r["payment"])) for r in rows]
    assert produced == _ffiec_payment_sequence(v), (
        "the FFIEC vector no longer matches what this system discloses for this "
        "loan -- recapture it before relying on the recorded APR"
    )
    assert sum(produced) == v["total_of_payments"]


@pytest.mark.skipif(
    FFIEC_VECTOR["expected_apr"] is None,
    reason=(
        "FFIEC APR tool result not yet captured. This is the only OUTSIDE oracle in "
        "this file and PR #10 must not merge while it is skipped -- sections 1-3 are "
        "all internal to this repository. To close: run the FFIEC APR tool with "
        "amount financed 17,460.00, monthly, regular first period, no balloon, and "
        "the payment stream entered as 47 payments of 439.35 FOLLOWED BY one "
        "payment of 439.25 (total 21,088.70 -- check the tool agrees). Then fill in "
        "tool, url, expected_apr and verified_on. Do not enter 48 level payments: "
        "that is a different cash flow and its APR would not be this loan's."
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
    # Solved over the actual sequence, through the same function production
    # uses. compute_apr's level-payment path is deliberately NOT used here: it
    # prices a cash flow with no adjusted final payment, so agreeing with the
    # tool through it would prove nothing about what is disclosed.
    disclosed = Decimal(str(apr.apr_from_cash_flows(
        v["amount_financed"], _ffiec_payment_sequence(v)
    )))

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
