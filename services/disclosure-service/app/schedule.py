"""Amortization schedule generation (for the disclosure / payment schedule display).

Standard fixed-payment amortization. Decimal throughout the accumulation loop
(via apr.monthly_payment_decimal()), matching the same fix applied to apr.py --
this used to accumulate in float across up to 60 rows, the same drift that
affected the disclosed APR.
"""
import datetime
from decimal import ROUND_HALF_UP, Decimal

from . import apr


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


def amortization(principal: float, annual_rate_pct: float, term_months: int,
                 start: datetime.date | None = None) -> list[dict]:
    start = start or datetime.date.today()
    p = Decimal(str(principal))
    regular = _cents(apr.monthly_payment_decimal(principal, annual_rate_pct, term_months))
    monthly_rate = Decimal(str(annual_rate_pct)) / 100 / 12
    balance = p
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


def _add_months(d: datetime.date, months: int) -> datetime.date:
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, 28)
    return datetime.date(year, month, day)
