"""Regenerate the actuarial-APR lookup table used by db/init/003_seed_bulk.sql.

SUPERSEDED -- DO NOT USE UNTIL REWRITTEN FOR MODEL B.
    This keys APR by (fee, note rate, term), which was sound while the schedule
    billed identical level payments: amount financed and payment both scaled
    linearly in principal, so principal cancelled out.

    Model B breaks that. The final payment absorbs each principal's own cent
    residue, so the cash-flow sequence -- and therefore the APR -- differs by
    principal. Measured spread across the seeded range: up to 0.003pp
    (7.99%/48mo gives 9.582 at P=1,040 and 9.584 at P=15,000).

    The replacement generates one APR per app_id from that row's actual
    cent-rounded schedule. Until it lands, this file is kept only for the
    cross-check helpers below.


WHEN TO RUN THIS
    Whenever the origination fee changes, a new note rate is introduced, or a new
    term appears in the bulk seeds. The seed will refuse to load otherwise: its
    DO block raises if any (fee, note rate, term) combination has no verified
    value, rather than silently seeding fewer offers.

WHY A LOOKUP AND NOT SQL ARITHMETIC
    The actuarial APR needs an iterative solve. Implementing one in seed SQL
    would be a second APR implementation to keep correct, so the values are
    generated here instead -- and re-derived from the seeded payment stream by
    db/tests/test_seed_offer_consistency.py on every CI run, so a stale literal
    fails the build rather than shipping.

WHY PRINCIPAL IS NOT A KEY
    With a proportional prepaid fee the APR is independent of principal: amount
    financed and payment both scale linearly in P, so P cancels out of the
    present-value equation. Verified to 46 decimal places across a 48x principal
    range. The key is therefore (fee, note rate, term) only.

HOW TO RUN
    From the repository root:

        python db/tools/regenerate_seed_apr_lookup.py

    It prints the VALUES rows to stdout. Paste them over the existing rows in the
    apr_lookup CTE in db/init/003_seed_bulk.sql, then:

        docker compose down -v && docker compose up -d --build
        DATABASE_URL=... python -m pytest db/tests/test_seed_offer_consistency.py -q

    Every value is cross-checked here three ways before it is printed --
    bisection, Newton-Raphson, and disclosure-service's own compute_apr. The
    script exits non-zero if any of the three disagree beyond 0.001, so a bad
    value cannot reach the seed silently.

EDIT THESE WHEN THE SEED GENERATOR CHANGES
    FEE_PCT, RATES and TERMS below must match what db/init/003_seed_bulk.sql
    actually produces (rates come from `7.99 + (a.id % 16)`, terms from
    applications.term_months).
"""
import sys
from decimal import Decimal, ROUND_HALF_UP, getcontext
from pathlib import Path

getcontext().prec = 50

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "services" / "disclosure-service"))
from app import apr as production  # noqa: E402

FEE_PCT = Decimal("0.030")
RATES = [Decimal("7.99") + k for k in range(16)]
TERMS = [12, 48, 60]
REFERENCE_PRINCIPAL = Decimal("15000")   # any value; the APR does not depend on it
AGREEMENT_TOLERANCE = Decimal("0.001")   # one unit in the last disclosed place


def payment(principal: Decimal, note_rate_pct: Decimal, term: int) -> Decimal:
    i = note_rate_pct / 100 / 12
    if i == 0:
        return principal / term          # never the amortization formula at zero
    factor = (1 + i) ** term
    return principal * i * factor / (factor - 1)


def apr_by_bisection(amount_financed: Decimal, pmt: Decimal, term: int) -> Decimal:
    lo, hi = Decimal(0), Decimal(1)
    for _ in range(400):
        mid = (lo + hi) / 2
        pv = pmt * (1 - (1 + mid) ** -term) / mid if mid > 0 else pmt * term
        if pv > amount_financed:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2 * 12 * 100


def apr_by_newton(amount_financed: Decimal, pmt: Decimal, term: int) -> Decimal:
    i, h = Decimal("0.01"), Decimal("1E-12")
    for _ in range(200):
        f = pmt * (1 - (1 + i) ** -term) / i - amount_financed
        fp = ((pmt * (1 - (1 + i + h) ** -term) / (i + h) - amount_financed) - f) / h
        if fp == 0:
            break
        step = f / fp
        i -= step
        if abs(step) < Decimal("1E-30"):
            break
    return i * 12 * 100


def main() -> int:
    rows, failures = [], []
    for rate in RATES:
        for term in TERMS:
            af = REFERENCE_PRINCIPAL * (1 - FEE_PCT)
            pmt = payment(REFERENCE_PRINCIPAL, rate, term)
            bisect = apr_by_bisection(af, pmt, term).quantize(Decimal("0.001"), ROUND_HALF_UP)
            newton = apr_by_newton(af, pmt, term).quantize(Decimal("0.001"), ROUND_HALF_UP)
            prod = Decimal(str(production.compute_apr(
                float(REFERENCE_PRINCIPAL), float(rate), term)))
            if abs(bisect - newton) > AGREEMENT_TOLERANCE or abs(bisect - prod) > AGREEMENT_TOLERANCE:
                failures.append((rate, term, bisect, newton, prod))
            rows.append((FEE_PCT, rate, term, bisect))

    if failures:
        print("REFUSING TO EMIT -- the three methods disagree:", file=sys.stderr)
        for rate, term, b, n, p in failures:
            print(f"  {rate}% x{term}: bisection {b}, newton {n}, compute_apr {p}", file=sys.stderr)
        return 1

    print(f"-- {len(rows)} combinations, fee {FEE_PCT}, "
          f"cross-checked bisection / Newton-Raphson / compute_apr (all agree to 3dp).")
    print("-- Regenerate with: python db/tools/regenerate_seed_apr_lookup.py")
    for idx, (fee, rate, term, value) in enumerate(rows):
        print(f"    ({fee}, {rate}, {term}, {value}){',' if idx < len(rows) - 1 else ''}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
