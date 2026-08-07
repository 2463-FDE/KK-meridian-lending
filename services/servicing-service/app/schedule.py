"""Amortization schedule (display).

Decimal throughout the accumulation loop now (D12 fix, same pattern as
disclosure-service's schedule.py) -- this used to accumulate in float across
up to 60 rows.
"""
import datetime
from decimal import Decimal


def _to_decimal(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


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
    regular = round(pmt, 2)
    balance = _to_decimal(principal)
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
