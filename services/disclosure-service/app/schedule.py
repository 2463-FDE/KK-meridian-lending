"""Amortization schedule generation (for the disclosure / payment schedule display).

Standard fixed-payment amortization. Decimal throughout the accumulation loop
(via apr.monthly_payment_decimal()), matching the same fix applied to apr.py --
this used to accumulate in float across up to 60 rows, the same drift that
affected the disclosed APR.
"""
import datetime
from decimal import Decimal

from . import apr


def amortization(principal: float, annual_rate_pct: float, term_months: int,
                 start: datetime.date | None = None) -> list[dict]:
    start = start or datetime.date.today()
    p = Decimal(str(principal))
    regular = round(apr.monthly_payment_decimal(principal, annual_rate_pct, term_months), 2)
    monthly_rate = Decimal(str(annual_rate_pct)) / 100 / 12
    balance = p
    rows: list[dict] = []
    for n in range(1, term_months + 1):
        interest = round(balance * monthly_rate, 2)
        if n == term_months:
            principal_part = balance
            payment = round(principal_part + interest, 2)
        else:
            payment = regular
            principal_part = payment - interest
        balance = round(balance - principal_part, 2)
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
