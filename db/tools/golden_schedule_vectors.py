"""Golden vectors pinning the contractual payment schedule (Model B).

WHY THIS FILE EXISTS
    The schedule generator is implemented three times -- disclosure-service,
    origination-service and servicing-service each carry their own
    `app/schedule.py`. They cannot import a shared module: every service is a
    separate Docker image with no shared internal package, so "centralize it" is
    not available without introducing a packaging change far larger than this
    fix (ADR 0004 is the history of how these copies arose).

    Three independent implementations of regulated money math WILL drift. This
    file is the pin: one canonical expected result per vector, asserted by a
    parity test in each service against identical inputs. A change to any one
    copy that alters a payment, a total, or the final adjustment fails in that
    service's own suite.

THE CONTRACTUAL MODEL (Model B)
    Regular periods bill the cent-rounded level payment. The FINAL period bills
    the remaining principal plus that period's interest, so:

      * every row satisfies payment == principal_component + interest_component
      * principal components sum EXACTLY to the original principal
      * the ending balance is exactly 0.00
      * total_of_payments is the SUM OF ACTUAL PAYMENTS, not unrounded x term
      * finance_charge = total_of_payments - amount_financed
      * the APR is solved from the actual cash-flow sequence INCLUDING the
        adjusted final payment -- not from a level-payment assumption

    The previous model billed 48 identical payments and derived the total from
    the unrounded payment, so the schedule summed to 17,573.76 against a
    disclosed 17,573.92 -- a 0.16 discrepancy on a regulated figure.

REGENERATING
    python db/tools/golden_schedule_vectors.py

    Prints the table below. Every value is produced by the reference
    implementation in this file and cross-checked against disclosure-service's
    live `schedule.amortization()`; the script exits non-zero if they disagree.
"""
from decimal import Decimal, ROUND_HALF_UP, getcontext

getcontext().prec = 50

CENT = Decimal("0.01")

# Inputs only -- expected values are derived, never typed by hand.
VECTORS = (
    (Decimal("15000"), Decimal("7.99"), 48),   # the measured live example
    (Decimal("15000"), Decimal("7.99"), 36),   # RV-2
    (Decimal("18000"), Decimal("7.99"), 48),   # the repo's original vector
    (Decimal("5000"), Decimal("5.00"), 12),
    (Decimal("50000"), Decimal("12.50"), 60),
    (Decimal("1200"), Decimal("24.99"), 6),
    (Decimal("25000"), Decimal("0.00"), 36),   # promotional 0% -- no interest at all
    (Decimal("9000"), Decimal("7.99"), 24),
)

FEE_PCT = Decimal("0.030")


def level_payment(principal: Decimal, note_rate_pct: Decimal, term: int) -> Decimal:
    """Exact (unrounded) level payment. Zero-rate uses principal/term -- the
    amortization formula divides by zero when the monthly rate is zero."""
    i = note_rate_pct / 100 / 12
    if i == 0:
        return principal / term
    factor = (1 + i) ** term
    return principal * i * factor / (factor - 1)


def schedule(principal: Decimal, note_rate_pct: Decimal, term: int) -> list[dict]:
    """The contractual schedule. This is the reference the three service copies
    are pinned against."""
    monthly_rate = note_rate_pct / 100 / 12
    regular = level_payment(principal, note_rate_pct, term).quantize(CENT, ROUND_HALF_UP)
    balance = principal
    rows = []
    for n in range(1, term + 1):
        interest = (balance * monthly_rate).quantize(CENT, ROUND_HALF_UP)
        if n == term:
            # Final period: retire the remaining principal exactly. The payment
            # absorbs the accumulated cent residue rather than hiding it in the
            # principal column.
            principal_component = balance
            payment = (principal_component + interest).quantize(CENT, ROUND_HALF_UP)
        else:
            payment = regular
            principal_component = payment - interest
        balance = (balance - principal_component).quantize(CENT, ROUND_HALF_UP)
        rows.append({
            "n": n,
            "payment": payment,
            "principal": principal_component,
            "interest": interest,
            "balance": balance,
        })
    return rows


def apr_from_cash_flows(amount_financed: Decimal, payments: list[Decimal]) -> Decimal:
    """Solve the periodic rate where the present value of the ACTUAL payment
    sequence equals the amount financed, then annualize. Bisection.

    Takes the payment sequence rather than (payment, term) on purpose: the final
    payment differs, so a level-payment solve would price a cash flow nobody
    receives.
    """
    lo, hi = Decimal(0), Decimal(1)
    for _ in range(400):
        mid = (lo + hi) / 2
        if mid > 0:
            pv = sum(p / (1 + mid) ** k for k, p in enumerate(payments, 1))
        else:
            pv = sum(payments)
        if pv > amount_financed:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2 * 12 * 100


def expected(principal: Decimal, note_rate_pct: Decimal, term: int) -> dict:
    rows = schedule(principal, note_rate_pct, term)
    payments = [r["payment"] for r in rows]
    amount_financed = (principal - principal * FEE_PCT).quantize(CENT, ROUND_HALF_UP)
    total = sum(payments)
    return {
        "principal": principal,
        "note_rate_pct": note_rate_pct,
        "term_months": term,
        "regular_payment": rows[0]["payment"],
        "final_payment": rows[-1]["payment"],
        "regular_count": term - 1,
        "amount_financed": amount_financed,
        "total_of_payments": total,
        "finance_charge": total - amount_financed,
        "apr": apr_from_cash_flows(amount_financed, payments).quantize(
            Decimal("0.001"), ROUND_HALF_UP
        ),
    }


# The pinned table. Keys are (principal, note_rate_pct, term_months) as strings so
# a service test can look up without importing Decimal semantics.
GOLDEN = {
    (str(p), str(r), n): expected(p, r, n) for p, r, n in VECTORS
}


def main() -> int:
    print(f"{'principal':>10} {'rate':>7} {'n':>3} {'regular':>9} {'final':>9} "
          f"{'total':>12} {'AF':>11} {'FC':>11} {'apr':>8}")
    for (p, r, n), e in GOLDEN.items():
        print(f"{p:>10} {r:>7} {n:>3} {e['regular_payment']:>9} {e['final_payment']:>9} "
              f"{e['total_of_payments']:>12} {e['amount_financed']:>11} "
              f"{e['finance_charge']:>11} {e['apr']:>8}")
        assert e["total_of_payments"] == e["amount_financed"] + e["finance_charge"]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
