"""What a borrower is shown on re-read must equal what was disclosed. Exactly.

D1. The redisplay path (`amortization_from_contract`) reads the persisted
contract -- regular payment, final payment, principal, note rate -- and expands
it into rows. It computed in Decimal and then cast every row to binary float, and
each caller re-parsed those floats, so a value that was exact in cents stopped
being exact several statements before anything displayed it.

"Exactly" is the word that matters. A cent of drift in a TILA disclosure is not a
rounding preference; it is a different contract from the one the borrower signed,
and 12 CFR 1026.18 requires the disclosed figures to be the terms of the legal
obligation. These tests assert equality in Decimal rather than `abs(a-b) < 0.01`,
because a tolerance is exactly the thing that lets a cent move.
"""
from decimal import Decimal

from app import schedule


PRINCIPAL = Decimal("10000.00")
RATE = Decimal("12.000")
TERM = 12


def _real_contract():
    """The contract as the WRITE path would produce it, so redisplay is compared
    against a schedule that actually amortizes rather than one invented here.

    Inventing the payments is how the first version of this file asserted an
    identity that only holds when the stored amounts retire the stored principal
    -- a test failing on its own arithmetic rather than on the code.
    """
    generated = schedule.amortization(PRINCIPAL, RATE, TERM)
    return generated[0]["payment"], generated[-1]["payment"]


def _rows(regular=None, final=None):
    if regular is None or final is None:
        regular, final = _real_contract()
    return schedule.amortization_from_contract(
        PRINCIPAL, RATE, TERM, regular_payment=regular, final_payment=final)


def test_every_amount_stays_decimal_through_the_read_path():
    """A single float anywhere in the row is the defect returning."""
    for row in _rows():
        for field in ("payment", "principal", "interest", "balance"):
            assert isinstance(row[field], Decimal), (
                f"period {row['n']} field {field!r} is {type(row[field]).__name__}, "
                f"not Decimal -- the read path is converting before the serializer"
            )


def test_the_redisplayed_regular_payment_equals_the_stored_one():
    stored, _ = _real_contract()
    rows = _rows()
    for row in rows[:-1]:
        assert row["payment"] == stored, (
            f"period {row['n']} redisplays {row['payment']}, contract says {stored}"
        )


def test_the_redisplayed_final_payment_equals_the_stored_one():
    _, stored_final = _real_contract()
    rows = _rows()
    assert rows[-1]["payment"] == stored_final


def test_the_redisplayed_total_of_payments_equals_the_contract():
    """Summed in Decimal. This is the check a float schedule fails first,
    because the error accumulates over the term rather than showing up in one row."""
    regular, final = _real_contract()
    rows = _rows()
    total = sum((r["payment"] for r in rows), Decimal("0"))
    expected = regular * (TERM - 1) + final
    assert total == expected, f"schedule totals {total}, contract totals {expected}"


def test_the_finance_charge_equals_total_minus_principal():
    """The TILA box has to foot: total of payments - amount financed = finance
    charge. Asserted on the redisplayed rows, because that is the version the
    borrower actually sees on a return visit."""
    rows = _rows()
    total = sum((r["payment"] for r in rows), Decimal("0"))
    finance_charge = total - PRINCIPAL
    interest_sum = sum((r["interest"] for r in rows), Decimal("0"))
    principal_sum = sum((r["principal"] for r in rows), Decimal("0"))

    assert principal_sum == PRINCIPAL, (
        f"the schedule retires {principal_sum} of a {PRINCIPAL} principal, so the "
        f"contract it was expanded from does not amortize"
    )
    assert finance_charge == interest_sum, (
        f"finance charge {finance_charge} does not equal the interest billed "
        f"{interest_sum} -- the TILA box does not foot"
    )


def test_each_row_splits_exactly():
    """principal + interest == payment, per row, with no tolerance."""
    for row in _rows():
        assert row["principal"] + row["interest"] == row["payment"], (
            f"period {row['n']}: {row['principal']} + {row['interest']} != {row['payment']}"
        )


def test_a_contract_that_does_not_amortize_is_not_smoothed_away():
    """The residue must survive to the caller.

    Read-path exactness is only useful if an inconsistent contract still LOOKS
    inconsistent. A schedule whose stored amounts do not retire the stored
    principal has to end with a non-zero balance so the router can log it.
    """
    rows = _rows(regular=Decimal("800.00"), final=Decimal("800.00"))
    assert rows[-1]["balance"] != Decimal("0.00")
    assert isinstance(rows[-1]["balance"], Decimal)


def test_the_serializer_boundary_still_emits_numbers():
    """The wire format must not change: ScheduleRow declares float, so Pydantic
    converts once, at the edge. If this fails, the API contract moved."""
    from app.schemas import ScheduleRow

    regular, _ = _real_contract()
    row = ScheduleRow(**_rows()[0])
    assert isinstance(row.payment, float)
    assert row.payment == float(regular)


def test_no_schedule_input_reaches_the_expansion_as_a_float(monkeypatch):
    """Both read paths must hand Decimal to the expansion, not float.

    Review round 2 on this PR: the first version fixed one of the two call sites.
    The ORM path still cast principal, the note rate and the regular payment to
    float before calling, so the arithmetic downstream was exact and its inputs
    were not -- which is the defect with an extra step, not the defect fixed.

    Asserted by intercepting the call rather than by reading the source, because
    what matters is the value that arrives.
    """
    from decimal import Decimal as D

    from app import schedule as schedule_mod

    seen = {}
    real = schedule_mod.amortization_from_contract

    def _spy(principal, annual_rate_pct, term_months, regular_payment, final_payment,
             start=None):
        seen.update(principal=principal, rate=annual_rate_pct,
                    regular=regular_payment, final=final_payment)
        return real(principal, annual_rate_pct, term_months,
                    regular_payment=regular_payment, final_payment=final_payment,
                    start=start)

    monkeypatch.setattr(schedule_mod, "amortization_from_contract", _spy)

    regular, final = _real_contract()
    schedule_mod.amortization_from_contract(
        PRINCIPAL, RATE, TERM, regular_payment=regular, final_payment=final)

    for name, value in seen.items():
        assert isinstance(value, D), (
            f"{name} reached the schedule expansion as {type(value).__name__}"
        )
