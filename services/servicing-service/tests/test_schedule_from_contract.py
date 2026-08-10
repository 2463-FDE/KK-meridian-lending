"""Servicing bills the schedule that was boarded, and says so when it cannot.

The defect: `loan_schedule` always called `amortization(principal, apr, term)`,
which solves the payment at read time. That made an accepted disclosure
something the system recomputed rather than something it stored -- a later
rounding-policy or fee change silently re-wrote the terms of a loan somebody
had already signed. Under Model B the recomputation cannot even reproduce the
contract: the final payment absorbs the cent residue and is not a function of
principal, rate and term.

Two behaviours are asserted here, and the second matters as much as the first:

  * a loan WITH a stored schedule is billed those exact amounts;
  * a loan WITHOUT one (boarded before db/migrations/0030, which deliberately
    does not back-fill) is reconstructed and labelled as a reconstruction.
    Presenting a reconstruction as the agreed terms would be a guess wearing a
    contract's clothes.

Note on fixtures. These use stub loan objects, which is the same construction
that let an undeclared ORM column go unnoticed for a whole PR -- a stub carries
whatever attributes the test sets. That gap is closed separately and
deliberately: test_loan_model_mapping.py asserts the columns read below are
actually mapped, so a stub here cannot pass on a field Postgres would return as
None. What is being tested here is branch and arithmetic behaviour.
"""
from types import SimpleNamespace

import pytest

from app import schedule
from app.routers.loans import loan_schedule

# A self-consistent B1 contract: 9000 at 7.99% over 24 months bills 407.00 for
# 23 periods and 406.85 in the final one. The regular payment is the cent-
# rounded level payment; the final payment is remaining principal plus that
# period's interest, which is why it differs.
_PRINCIPAL = 9000.00
_NOTE_RATE = 7.99
_TERM = 24


def _contract_loan(**overrides):
    base = dict(
        id=1, app_id=1, principal=_PRINCIPAL, apr=_NOTE_RATE, term_months=_TERM,
        regular_payment=None, regular_payment_count=_TERM - 1,
        final_payment=None, schedule_version="B1", status="current",
    )
    # Derive the stored amounts from the generator so the fixture is a real B1
    # contract rather than hand-typed numbers that may not amortize.
    rows = schedule.amortization(_PRINCIPAL, _NOTE_RATE, _TERM)
    base["regular_payment"] = rows[0]["payment"]
    base["final_payment"] = rows[-1]["payment"]
    base.update(overrides)
    return SimpleNamespace(**base)


def _legacy_loan(**overrides):
    """Boarded before 0030: all four schedule columns NULL together, which
    loans_schedule_all_or_nothing makes the only legal absent state."""
    base = dict(
        id=2, app_id=2, principal=_PRINCIPAL, apr=_NOTE_RATE, term_months=_TERM,
        regular_payment=None, regular_payment_count=None,
        final_payment=None, schedule_version=None, status="current",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class _Session:
    def __init__(self, loan):
        self._loan = loan

    def get(self, _model, loan_id):
        return self._loan if self._loan and self._loan.id == loan_id else None


def _call(loan):
    return loan_schedule(loan.id, session=_Session(loan))


# --------------------------------------------------------------------------
# a boarded contract is billed as recorded
# --------------------------------------------------------------------------

def test_a_stored_schedule_is_billed_exactly_as_recorded():
    loan = _contract_loan()
    out = _call(loan)

    assert out.source == "contract"
    assert out.schedule_version == "B1"
    assert out.note is None, "a consistent contract needs no caveat"
    assert out.unamortized_residue is None

    assert len(out.schedule) == _TERM
    for row in out.schedule[:-1]:
        assert row.payment == loan.regular_payment
    assert out.schedule[-1].payment == loan.final_payment


def test_the_final_payment_is_the_stored_one_not_a_recomputed_one():
    """The whole reason the schedule is persisted.

    The stored final payment is moved a cent away from the generator's own
    answer. Nothing about principal, rate or term changes -- so a read path
    that recomputes cannot notice, and one that bills what was recorded must.
    """
    loan = _contract_loan()
    tampered = round(loan.final_payment + 0.01, 2)
    loan.final_payment = tampered

    out = _call(loan)

    assert out.schedule[-1].payment == tampered, (
        "billed a recomputed final payment instead of the recorded one"
    )


def test_the_total_billed_equals_the_recorded_terms():
    """regular x count + final. If this holds, the borrower is billed the
    contract and nothing else -- no cent invented, none dropped."""
    loan = _contract_loan()
    total = round(sum(r.payment for r in _call(loan).schedule), 2)
    expected = round(loan.regular_payment * (loan.term_months - 1) + loan.final_payment, 2)
    assert total == expected


def test_a_consistent_contract_closes_at_zero():
    """The stored amounts amortize the principal exactly, so the last balance is
    zero -- and it is zero because the arithmetic worked, not because the
    function clamps it. amortization_from_contract deliberately does not clamp."""
    out = _call(_contract_loan())
    assert out.schedule[-1].balance == 0.0


def test_interest_is_still_computed_and_is_not_taken_from_the_stored_amount():
    """Only the payment amounts are contractual. The interest/principal split is
    arithmetic on the note rate, so it is computed -- storing a split would be
    storing a derivation."""
    loan = _contract_loan()
    first = _call(loan).schedule[0]
    # 9000 x 7.99% / 12 = 59.925 exactly -- an exact half-cent, so the expected
    # value is a literal rather than a round() call. This test originally used
    # round(), which is round-half-to-EVEN and yields 59.92; that is the same
    # rounding-mode bug the generators were just corrected for, so computing the
    # expectation that way would have asserted the defect.
    assert first.interest == 59.93
    assert round(first.principal + first.interest, 2) == loan.regular_payment


# --------------------------------------------------------------------------
# an inconsistent contract is surfaced, not absorbed
# --------------------------------------------------------------------------

def test_stored_amounts_that_do_not_amortize_the_principal_are_reported():
    """A residue means the recorded contract and the recorded principal
    disagree. The amounts on record are still what is shown -- they are the
    contract -- but the discrepancy is stated rather than clamped away, because
    silently absorbing it is indistinguishable from the drift being prevented.
    """
    loan = _contract_loan()
    loan.final_payment = round(loan.final_payment - 5.00, 2)

    out = _call(loan)

    assert out.source == "contract"
    assert out.unamortized_residue is not None
    assert out.unamortized_residue == pytest.approx(5.00, abs=0.01)
    assert "do not fully amortize" in out.note
    # The shown amounts are still the recorded ones -- reporting the problem
    # must not also mean quietly correcting it.
    assert out.schedule[-1].payment == loan.final_payment


def test_a_one_cent_residue_is_still_reported():
    """The threshold is a cent, not a dollar. A cent that cannot be accounted
    for is a cent somebody either owes or does not."""
    loan = _contract_loan()
    loan.final_payment = round(loan.final_payment - 0.01, 2)
    out = _call(loan)
    assert out.unamortized_residue == pytest.approx(0.01, abs=0.001)
    assert out.note is not None


# --------------------------------------------------------------------------
# legacy: reconstructed, and labelled as such
# --------------------------------------------------------------------------

def test_a_legacy_loan_still_gets_a_schedule():
    """Withholding it would leave a borrower unable to see what they owe."""
    out = _call(_legacy_loan())
    assert len(out.schedule) == _TERM
    assert out.schedule[0].payment > 0


def test_a_legacy_schedule_is_labelled_a_reconstruction():
    """The specific claim that must never be made: that these are the agreed
    terms. There is no stored version to name, so schedule_version stays None
    rather than being filled in with the generator's current policy -- which
    would assert the loan was written under a policy that postdates it."""
    out = _call(_legacy_loan())

    assert out.source == "reconstructed"
    assert out.schedule_version is None
    assert out.note is not None
    assert "reconstructed" in out.note.lower()
    assert "not the" in out.note.lower(), (
        "the note must deny that these are the agreed terms, not merely "
        "describe how they were produced"
    )


def test_the_two_sources_are_distinguishable_without_reading_the_note():
    """A caller deciding whether it may present these as contractual terms has
    to be able to branch on a field, not parse prose."""
    assert _call(_contract_loan()).source == "contract"
    assert _call(_legacy_loan()).source == "reconstructed"


def test_a_legacy_reconstruction_matches_the_display_generator():
    """The reconstruction is the current generator's answer -- stated plainly so
    that if it ever silently diverged from `amortization`, this would fail
    rather than the difference being invisible behind the caveat."""
    loan = _legacy_loan()
    out = _call(loan)
    expected = schedule.amortization(loan.principal, loan.apr, loan.term_months)
    assert [r.payment for r in out.schedule] == [r["payment"] for r in expected]


def test_a_missing_loan_is_still_a_404():
    """The new branching must not have turned a missing loan into an empty
    schedule."""
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as exc:
        loan_schedule(999, session=_Session(None))
    assert exc.value.status_code == 404
