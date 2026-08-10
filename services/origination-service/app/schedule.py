"""Amortization schedule generation (for the disclosure / payment schedule display).

Decimal throughout, rounded HALF-UP at every cent boundary. Both parts of that
sentence are load-bearing and both were wrong here:

  * This copy used to do the whole accumulation in float, described in its own
    docstring as "fine for a display schedule". It is not only a display
    schedule any more -- origination writes loans.regular_payment at boarding
    from these terms, so a cent of float drift is a cent a borrower is billed.

  * Every cent boundary used Python's round(), which is round-half-to-EVEN.
    Postgres NUMERIC rounds half away from zero, so a value computed here and
    the same value computed in SQL could differ by a cent on an exact tie.
    Caught by the BOARDING-24 golden vector: 9000 at 7.99% has a first-period
    interest of exactly 59.925, which half-to-even makes 59.92 and half-up
    makes 59.93. Every subsequent row inherits the difference.

This generator must stay identical in behaviour to disclosure-service's and
servicing-service's copies -- three separate images with no shared package
(ADR 0004). Pinned by db/golden/model_b_schedule_vectors.json and
tests/test_golden_schedule_parity.py in each service.
"""
import datetime
from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def _to_decimal(value) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _cents(value) -> Decimal:
    """Round to cents HALF-UP, matching Postgres NUMERIC.

    Not round(): round() on a Decimal is half-to-even, so an exact half-cent
    goes to the nearest even digit and disagrees with the database that stores
    the result.
    """
    return _to_decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def _monthly_payment(principal, annual_rate_pct, term_months: int) -> Decimal:
    """The exact (unrounded) level payment. Zero rate uses principal/term --
    the amortization formula divides by zero when the monthly rate is zero."""
    p = _to_decimal(principal)
    r = _to_decimal(annual_rate_pct) / 100 / 12
    if r == 0:
        return p / term_months
    factor = (1 + r) ** term_months
    return p * r * factor / (factor - 1)


def amortization(principal, annual_rate_pct, term_months: int,
                 start: datetime.date | None = None) -> list[dict]:
    start = start or datetime.date.today()
    pmt = _monthly_payment(principal, annual_rate_pct, term_months)
    monthly_rate = _to_decimal(annual_rate_pct) / 100 / 12
    # Model B: regular periods bill the cent-rounded level payment; the final
    # period bills remaining principal plus that period's interest, so the
    # payment absorbs the accumulated cent residue rather than the principal
    # column hiding it.
    regular = _cents(pmt)
    balance = _cents(principal)
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
