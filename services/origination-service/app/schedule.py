"""Amortization schedule generation (for the disclosure / payment schedule display).

Standard fixed-payment amortization. Uses float math (consistent with the rest of the
platform); fine for a display schedule, but the same float drift that affects apr.py
applies here too.
"""
import datetime


def _monthly_payment(principal: float, annual_rate_pct: float, term_months: int) -> float:
    # Inlined from the (now-removed) apr.py — APR/finance-charge moved to disclosure-service,
    # but the display schedule generator stays in the LOS. Float math (D1) preserved.
    r = (annual_rate_pct / 100.0) / 12.0
    if r == 0:
        return principal / term_months
    factor = (1 + r) ** term_months
    return principal * r * factor / (factor - 1)


def amortization(principal: float, annual_rate_pct: float, term_months: int,
                 start: datetime.date | None = None) -> list[dict]:
    start = start or datetime.date.today()
    pmt = _monthly_payment(principal, annual_rate_pct, term_months)
    monthly_rate = (annual_rate_pct / 100.0) / 12.0
    # Model B: regular periods bill the cent-rounded level payment; the final
    # period bills remaining principal plus that period's interest. Same
    # behaviour as disclosure-service's and servicing-service's copies, pinned by
    # db/tools/golden_schedule_vectors.py and a parity test in each service.
    regular = round(pmt, 2)
    balance = round(principal, 2)
    rows: list[dict] = []
    for n in range(1, term_months + 1):
        interest = round(balance * monthly_rate, 2)
        if n == term_months:
            principal_part = round(balance, 2)
            payment = round(principal_part + interest, 2)
        else:
            payment = regular
            principal_part = round(payment - interest, 2)
        balance = round(balance - principal_part, 2)
        due = _add_months(start, n)
        rows.append({
            "n": n,
            "due_date": due.isoformat(),
            "payment": payment,
            "principal": principal_part,
            "interest": interest,
            "balance": round(max(balance, 0.0), 2),
        })
    return rows


def _add_months(d: datetime.date, months: int) -> datetime.date:
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, 28)
    return datetime.date(year, month, day)
