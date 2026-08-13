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
            # Decimal, not float. D1: these rows were cast to binary float here
            # and re-parsed by every caller, so a value that is exact in cents
            # stopped being exact several statements before anything displayed
            # it. The cast now happens once, at the serializer -- `ScheduleRow`
            # declares these as float, so the wire format is unchanged and only
            # the arithmetic in between is fixed.
            "payment": payment,
            "principal": principal_part,
            "interest": interest,
            "balance": max(balance, Decimal("0")),
        })
    return rows


def amortization_from_contract(principal, annual_rate_pct, term_months: int,
                               regular_payment, final_payment,
                               start: datetime.date | None = None) -> list[dict]:
    """Expand a STORED contract into rows, rather than re-deriving it.

    `amortization` above solves the payment from principal, rate and term. That
    is right when building an offer and wrong when redisplaying one: it rebuilds
    the contract with whatever generator is deployed at read time, so a later
    rounding-policy change silently alters the terms of a disclosure somebody
    has already been shown -- the drift `schedule_version` and the stored
    payment columns exist to prevent.

    An earlier version of the read path regenerated every row and then patched
    only the final one back to the stored value. That left the regular rows, and
    the corrected row's own principal/interest split, computed from the
    redeployed algorithm: a schedule whose last line agreed with the disclosure
    and whose body did not. Review finding on PR #10.

    Mirrors servicing-service's function of the same name, deliberately: the
    borrower's displayed schedule and the schedule servicing bills must be the
    same expansion of the same stored facts, or the two screens disagree.
    Amounts come from storage; only the interest split is computed, because the
    split is arithmetic on the contractual rate rather than a policy choice.
    """
    start = start or datetime.date.today()
    monthly_rate = Decimal(str(annual_rate_pct)) / 100 / 12
    regular = _cents(regular_payment)
    final = _cents(final_payment)
    balance = Decimal(str(principal))
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
            # Decimal to the serializer boundary (D1). This is the REDISPLAY
            # path: these amounts are read back from a disclosure the borrower
            # has already been shown, so a cent that moves here is a cent the
            # contract does not say.
            "payment": payment,
            "principal": principal_part,
            "interest": interest,
            # Not clamped to zero. If the stored amounts do not amortize the
            # stored principal, that is a real inconsistency and hiding it here
            # would defeat the point of storing them.
            "balance": balance,
        })
    return rows


def _add_months(d: datetime.date, months: int) -> datetime.date:
    month = d.month - 1 + months
    year = d.year + month // 12
    month = month % 12 + 1
    day = min(d.day, 28)
    return datetime.date(year, month, day)
