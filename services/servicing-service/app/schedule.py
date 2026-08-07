"""Amortization schedule (display).

Decimal throughout the accumulation loop now (D12 fix, same pattern as
disclosure-service's schedule.py) -- this used to accumulate in float across
up to 60 rows.
"""
import datetime
from decimal import ROUND_HALF_UP, Decimal


def _to_decimal(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


CENT = Decimal("0.01")


def _cents(value) -> Decimal:
    """Round to cents HALF-UP, matching Postgres NUMERIC.

    Not round(): round() on a Decimal is round-half-to-EVEN, so an exact
    half-cent goes to the nearest even digit and disagrees with the database
    that stores the result. Caught by the BOARDING-24 golden vector, whose
    first-period interest is exactly 59.925 -- half-to-even gives 59.92,
    half-up gives 59.93, and every later row inherits the difference.
    """
    v = value if isinstance(value, Decimal) else Decimal(str(value))
    return v.quantize(CENT, rounding=ROUND_HALF_UP)


def monthly_payment_decimal(principal, annual_rate_pct, term_months: int) -> Decimal:
    p = _to_decimal(principal)
    r = _to_decimal(annual_rate_pct) / 100 / 12
    if r == 0:
        return p / term_months
    factor = (1 + r) ** term_months
    return p * r * factor / (factor - 1)


def monthly_payment(principal: float, annual_rate_pct: float, term_months: int) -> float:
    return float(monthly_payment_decimal(principal, annual_rate_pct, term_months))


def amortization(principal: float, annual_rate_pct: float, term_months: int,
                 start: datetime.date | None = None) -> list[dict]:
    start = start or datetime.date.today()
    pmt = monthly_payment_decimal(principal, annual_rate_pct, term_months)
    monthly_rate = _to_decimal(annual_rate_pct) / 100 / 12
    # Model B: regular periods bill the cent-rounded level payment; the final
    # period bills remaining principal plus that period's interest, so the
    # payment absorbs the cent residue rather than the principal column hiding
    # it. Must stay byte-identical in behaviour to disclosure-service's copy --
    # pinned by db/tools/golden_schedule_vectors.py and the parity test in each
    # service. Servicing bills from this; a drift here bills terms nobody
    # disclosed.
    regular = _cents(pmt)
    balance = _to_decimal(principal)
    rows: list[dict] = []
    for n in range(1, term_months + 1):
        interest = _cents(balance * monthly_rate)
        if n == term_months:
            principal_part = balance
            payment = _cents(principal_part + interest)
        else:
            payment = regular
            principal_part = payment - interest
        balance = _cents(balance - principal_part)
        due = _add_months(start, n)
        rows.append({
            "n": n,
            "due_date": due.isoformat(),
            "payment": float(payment),
            "principal": float(principal_part),
            "interest": float(interest),
            "balance": float(max(balance, Decimal("0"))),
        })
    return rows


def amortization_from_contract(principal, annual_rate_pct, term_months: int,
                               regular_payment, final_payment,
                               start: datetime.date | None = None) -> list[dict]:
    """Bill the payment amounts STORED on the loan, not recomputed ones.

    `amortization` above solves the payment from principal, rate and term. That
    is correct when building an offer and wrong when billing one: it re-derives
    the contract at read time, so a later rounding-policy or fee change silently
    alters the terms of a loan somebody already signed. Under Model B it cannot
    even reproduce them -- the final payment absorbs the cent residue and is not
    a function of any other stored figure.

    So the amounts come from `loans.regular_payment` / `loans.final_payment`
    (db/migrations/0030) and only the interest split is computed, because the
    split is arithmetic on the contractual rate rather than a policy choice.

    The closing balance is deliberately NOT clamped to zero, unlike the display
    schedule above. If the stored amounts do not amortize the principal, that is
    a real inconsistency between the contract and the loan it was written for,
    and clamping it would hide exactly the drift this function exists to
    prevent. The caller reports the residue; see loans.py::loan_schedule.
    """
    start = start or datetime.date.today()
    monthly_rate = _to_decimal(annual_rate_pct) / 100 / 12
    regular = _to_decimal(regular_payment)
    final = _to_decimal(final_payment)
    balance = _to_decimal(principal)
    rows: list[dict] = []
    for n in range(1, term_months + 1):
        interest = _cents(balance * monthly_rate)
        payment = final if n == term_months else regular
        principal_part = payment - interest
        balance = _cents(balance - principal_part)
        due = _add_months(start, n)
        rows.append({
            "n": n,
            "due_date": due.isoformat(),
            "payment": float(payment),
            "principal": float(principal_part),
            "interest": float(interest),
            "balance": float(balance),
        })
    return rows


def _add_months(d: datetime.date, months: int) -> datetime.date:
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, 28)
    return datetime.date(year, month, day)
