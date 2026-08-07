"""Regenerate the seeded offer rows as exact Model B contracts, per application.

    python db/tools/regenerate_seed_offers.py            # check only, exit 1 on drift
    python db/tools/regenerate_seed_offers.py --write     # rewrite the seed SQL

WHY THIS REPLACED THE (fee, rate, term) APR LOOKUP
    The previous tool, regenerate_seed_apr_lookup.py, keyed every APR on
    (fee_pct, note_rate_pct, term_months). That key was valid under the old
    level-payment model, where amount financed and payment both scale linearly
    in principal so principal cancels out of the present-value equation -- 48
    combinations covered 180 rows exactly.

    Model B broke the key, not by approximation but in kind. The final payment
    absorbs the accumulated cent residue, and how large that residue is depends
    on the principal. So two loans at the same fee, rate and term now have
    genuinely different cash-flow sequences and genuinely different APRs. Across
    the 180 seeded rows the measured spread within a single (rate, term) group
    reaches 0.001pp at 3dp -- small, and not zero, which is the point: a lookup
    keyed without principal cannot express it, and collapsing each group to one
    value writes an APR that belongs to no row in it. The run prints this spread
    on every invocation rather than leaving it as a claim in a comment.

    This tool therefore emits ONE ROW PER APPLICATION. 180 bulk rows plus 4
    curated ones, each with its own solved APR and its own stored schedule.

DETERMINISM
    The bulk seed generates applications and loans arithmetically in SQL:

        amount      = 1000 + ((id * 263) % 49000)
        term_months = [12,24,36,48,60][(id * 3) % 5]
        status      = ['funded','funded','funded','decided','submitted'][(id*2)%5]
        note rate   = round(7.99 + (id % 16), 3)

    Those formulas are reproduced below rather than queried, so this tool needs
    no database and cannot be run against a drifted one by accident. If the seed
    formulas change, _bulk_applications() must change with them -- which is why
    the generated block is CHECKED, not merely written: a mismatch between this
    tool and the committed SQL fails CI through
    db/tests/test_seed_offer_consistency.py.

ROUNDING
    ROUND_HALF_UP at every cent boundary, matching Postgres NUMERIC and the
    three service schedule generators. Python's round() is half-to-EVEN and
    disagrees on an exact tie -- see db/golden/model_b_schedule_vectors.json,
    whose BOARDING-24 vector exists because of that difference.

THE APR VALUES ARE NOT INDEPENDENTLY CERTIFIED
    They are this system's own actuarial solve over the actual payment
    sequence. No outside oracle has confirmed them; the FFIEC check is still
    outstanding. They make the seed data self-consistent, which is what
    test_seed_offer_consistency.py verifies. They are not evidence that the
    solver is right.
"""
import argparse
import re
import sys
from decimal import ROUND_HALF_UP, Decimal, getcontext
from pathlib import Path

getcontext().prec = 50

CENT = Decimal("0.01")
MILLI = Decimal("0.001")
FEE_PCT = Decimal("0.030")

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BULK_SQL = REPO_ROOT / "db" / "init" / "003_seed_bulk.sql"
CURATED_SQL = REPO_ROOT / "db" / "init" / "002_seed.sql"

BEGIN = "-- BEGIN GENERATED OFFER ROWS (db/tools/regenerate_seed_offers.py)"
END = "-- END GENERATED OFFER ROWS"

TERMS = [12, 24, 36, 48, 60]
STATUSES = ["funded", "funded", "funded", "decided", "submitted"]

# The four curated anchors, as declared in db/init/002_seed.sql. Listed here as
# (app_id, principal, note_rate_pct, term_months) so this tool is the single
# place that computes their money.
CURATED = [
    (4471, Decimal("18000"), Decimal("7.99"), 48),
    (5582, Decimal("12000"), Decimal("9.99"), 36),
    (6011, Decimal("15000"), Decimal("7.99"), 36),
    (6014, Decimal("50000"), Decimal("11.25"), 60),
]


def _cents(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _bulk_applications():
    """(app_id, principal, note_rate_pct, term_months) for every FUNDED bulk app."""
    out = []
    for g in range(7000, 7300):
        if STATUSES[(g * 2) % 5] != "funded":
            continue
        principal = Decimal(1000 + ((g * 263) % 49000))
        term = TERMS[(g * 3) % 5]
        note_rate = (Decimal("7.99") + (g % 16)).quantize(MILLI)
        out.append((g, principal, note_rate, term))
    return out


def level_payment(principal: Decimal, note_rate_pct: Decimal, term: int) -> Decimal:
    i = note_rate_pct / 100 / 12
    if i == 0:
        return principal / term
    factor = (1 + i) ** term
    return principal * i * factor / (factor - 1)


def schedule(principal: Decimal, note_rate_pct: Decimal, term: int):
    """Model B: cent-rounded regular payments, adjusted final payment."""
    monthly_rate = note_rate_pct / 100 / 12
    regular = _cents(level_payment(principal, note_rate_pct, term))
    balance = principal
    payments = []
    for n in range(1, term + 1):
        interest = _cents(balance * monthly_rate)
        if n == term:
            principal_part = balance
            payment = _cents(principal_part + interest)
        else:
            payment = regular
            principal_part = payment - interest
        balance = _cents(balance - principal_part)
        payments.append(payment)
    assert balance == 0, f"schedule did not close: {balance}"
    return regular, payments


def apr_from_cash_flows(amount_financed: Decimal, payments) -> Decimal:
    """Bisection on the periodic rate, over the ACTUAL payment sequence.

    Takes the sequence rather than (payment, term): the final payment differs,
    so a level-payment solve prices a cash flow nobody receives.
    """
    lo, hi = Decimal(0), Decimal(1)
    for _ in range(400):
        mid = (lo + hi) / 2
        pv = sum(p / (1 + mid) ** k for k, p in enumerate(payments, 1)) if mid > 0 else sum(payments)
        if pv > amount_financed:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2 * 12 * 100


def offer_row(app_id: int, principal: Decimal, note_rate_pct: Decimal, term: int) -> dict:
    regular, payments = schedule(principal, note_rate_pct, term)
    amount_financed = _cents(principal - principal * FEE_PCT)
    total = sum(payments)
    return {
        "app_id": app_id,
        "note_rate_pct": note_rate_pct,
        "apr": apr_from_cash_flows(amount_financed, payments).quantize(MILLI, ROUND_HALF_UP),
        "finance_charge": total - amount_financed,
        "monthly_payment": regular,
        "amount_financed": amount_financed,
        "total_of_payments": total,
        "regular_payment_count": term - 1,
        "final_payment": payments[-1],
        "term_months": term,
    }


def _values_line(r: dict) -> str:
    return (
        f"  ({r['app_id']}, {r['note_rate_pct']}, 0.0300, {r['apr']}, "
        f"{r['finance_charge']}, {r['monthly_payment']}, {r['amount_financed']}, "
        f"{r['total_of_payments']}, {r['regular_payment_count']}, "
        f"{r['final_payment']}, {r['term_months']}, 'B1')"
    )


def bulk_block() -> str:
    rows = [offer_row(*a) for a in _bulk_applications()]
    lines = [
        BEGIN,
        f"-- {len(rows)} rows, one per FUNDED bulk application. Do not hand-edit:",
        "-- regenerate with `python db/tools/regenerate_seed_offers.py --write`.",
        "--",
        "-- One row per APPLICATION, not per (fee, rate, term) combination. Model B's",
        "-- final payment absorbs a cent residue whose size depends on the principal,",
        "-- so two loans at the same rate and term have different cash flows and",
        "-- different APRs. The old lookup could not express that.",
        "INSERT INTO offers (app_id, note_rate_pct, fee_pct_used, apr,",
        "                    finance_charge, monthly_payment, amount_financed,",
        "                    total_of_payments, regular_payment_count, final_payment,",
        "                    term_months, schedule_version) VALUES",
    ]
    body = ",\n".join(_values_line(r) for r in rows)
    return "\n".join(lines) + "\n" + body + ";\n" + END


def curated_block() -> str:
    rows = [offer_row(*c) for c in CURATED]
    lines = [
        BEGIN,
        "-- The four curated anchors. Do not hand-edit: regenerate with",
        "-- `python db/tools/regenerate_seed_offers.py --write`.",
        "--",
        "-- These previously carried pre-Model-B values -- 4471's total was 21088.71",
        "-- against an actual 21088.70, and 6011's 16919.15 against 16919.17. A cent",
        "-- or two, on the figures a demo points at to show the disclosure footing.",
        "INSERT INTO offers (app_id, note_rate_pct, fee_pct_used, apr,",
        "                    finance_charge, monthly_payment, amount_financed,",
        "                    total_of_payments, regular_payment_count, final_payment,",
        "                    term_months, schedule_version) VALUES",
    ]
    body = ",\n".join(_values_line(r) for r in rows)
    return "\n".join(lines) + "\n" + body + ";\n" + END


def _replace_block(path: Path, block: str, write: bool) -> bool:
    text = path.read_text()
    pattern = re.compile(re.escape(BEGIN) + r".*?" + re.escape(END), re.DOTALL)
    if not pattern.search(text):
        print(f"{path}: no generated block found -- expected markers are missing")
        return False
    updated = pattern.sub(lambda _: block, text, count=1)
    if updated == text:
        print(f"{path}: up to date")
        return True
    if write:
        path.write_text(updated)
        print(f"{path}: rewritten")
        return True
    print(f"{path}: OUT OF DATE -- rerun with --write")
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--write", action="store_true", help="rewrite the seed SQL in place")
    args = ap.parse_args()

    bulk = _bulk_applications()
    print(f"{len(bulk)} funded bulk applications, {len(CURATED)} curated")

    # A spread report, because the whole reason this tool exists is that the APR
    # is no longer a function of (fee, rate, term) alone.
    by_key = {}
    for app_id, principal, rate, term in bulk:
        by_key.setdefault((rate, term), []).append(
            offer_row(app_id, principal, rate, term)["apr"]
        )
    spreads = [max(v) - min(v) for v in by_key.values() if len(v) > 1]
    if spreads:
        print(
            f"APR spread within identical (rate, term) groups: max "
            f"{max(spreads)}pp over {len(by_key)} groups -- this is why the old "
            f"(fee, rate, term) lookup was retired"
        )

    ok = _replace_block(BULK_SQL, bulk_block(), args.write)
    ok = _replace_block(CURATED_SQL, curated_block(), args.write) and ok
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
