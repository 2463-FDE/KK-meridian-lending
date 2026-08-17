"""Payment-history display tests.

ADR 0008 (Week 5 tokenization) stopped this system receiving card numbers at
all: a payment carries `last4` from the processor's token response, never a
PAN. `db/migrations/0029` back-fills `last4` from `pan` so historical payments
still display; the columns are dropped later, in the contract step (0031).

That back-fill is why the old "fall back to masking a full pan" case is gone
rather than merely deleted: every row that needed it now carries `last4`, so no
code path in this service reads a card number even though the column remains. The tests below pin
what remains true -- last4 is displayed when present, its absence renders
nothing rather than a guess, and nothing reads a card number.
"""
from types import SimpleNamespace

from app.routers.loans import _display_last4


def test_displays_the_stored_last4():
    payment = SimpleNamespace(last4="1111")
    assert _display_last4(payment) == "•••• 1111"


def test_a_row_with_no_last4_displays_nothing():
    """Renders nothing rather than a placeholder that could be mistaken for
    real card digits. Reachable only for a pre-tokenization row whose `pan` was
    too short for 0029's back-fill to take four digits from -- not for anything
    written since."""
    payment = SimpleNamespace(last4=None)
    assert _display_last4(payment) is None


def test_the_display_never_reads_a_pan_attribute():
    """Regression guard for the removed `pan` read. If a fallback is ever
    reinstated this fails loudly: the object raises on any attribute other than
    `last4`, so touching `.pan` is an error rather than a silent revival of
    card-number handling."""
    class _Last4Only:
        last4 = "4242"

        def __getattr__(self, name):
            raise AssertionError(
                f"_display_last4 read {name!r} -- the only card field this "
                f"service may touch is last4 (ADR 0008, db/migrations/0029)"
            )

    assert _display_last4(_Last4Only()) == "•••• 4242"


def test_a_row_with_a_legacy_pan_and_no_last4_still_shows_nothing():
    """The expand-phase fallback is GONE, and this is the case that proves it.

    PR #11 added a fallback for the deployment window where this service was live
    before 0029 had back-filled `last4`, and annotated it "remove in PR #15, the
    contract step". This is that step, so the row shape that fallback existed for
    -- `pan` populated, `last4` NULL -- must now render nothing instead of
    slicing digits out of a card number.

    That is safe rather than a regression because 0031 refuses to drop the
    columns until the back-fill is complete: a row can be in this shape before
    the contract step, not after it. The two tests that asserted the fallback's
    output were removed rather than inverted, because the behaviour they pinned
    no longer exists.
    """
    class _LegacyRow:
        last4 = None
        brand = "visa"
        pan = "4111111111111111"

    assert _display_last4(_LegacyRow()) is None


def test_last4_is_used_even_when_a_legacy_pan_is_present():
    """Precedence, asserted for a row carrying both.

    Paired with the test above so "reads last4" and "reads nothing at all" stay
    distinguishable: a display that had simply stopped working would pass that
    one and fail this one.
    """
    class _PostBackfillRow:
        last4 = "4242"
        brand = "visa"
        pan = "4111111111111111"

    assert _display_last4(_PostBackfillRow()) == "•••• 4242"
# --- a legacy loan's APR is not a note rate ----------------------------------

def test_every_loan_now_reports_a_proven_note_rate():
    """The rate is read from a column that can only hold one figure.

    **This test asserted the opposite until the D19 contract step, and the
    history is why it now reads this way.** `loans.apr` held two different
    regulated figures: the pre-change acceptance path copied `offers.apr` -- the
    DISCLOSED APR -- into it, 5.196% for a contract priced at 7.99%. An
    unconditional alias printed 5.196% to those borrowers as "Interest rate
    (note rate)", a contractual term they were never quoted, so this function
    reported a rate ONLY when `schedule_version` proved the current boarding
    path had written a contractual one, and this test pinned that refusal.

    Migration 0038 moved that inference into the data, and 0039 dropped `apr`
    and made `note_rate_pct` NOT NULL -- refusing to run while any loan lacked a
    proven rate. So the ambiguous column no longer exists and neither does the
    unproven case. Withholding a rate now would hide a number the borrower is
    entitled to see, on evidence (`schedule_version`) that no longer carries any
    meaning about which figure is stored.

    The enforcement moved with it: what stops an unknown rate is now the NOT NULL
    constraint, checked by `db/tests/test_0039_drop_loans_apr.py`, not a branch
    here. See `db/tests/test_note_rate_readers_agree.py` for the reader sweep.
    """
    from app.routers.loans import _proven_note_rate

    class _Loan:
        note_rate_pct = 5.196
        schedule_version = None

    rate, proven = _proven_note_rate(_Loan())
    assert rate == 5.196, (
        "a loan with no schedule_version reported no rate -- since the contract "
        "step there is no unproven rate to withhold, and withholding one would "
        "hide a number the borrower is entitled to see"
    )
    assert proven is True


def test_a_loan_boarded_with_its_contract_reports_the_note_rate():
    """The other half: a proven rate must still be shown."""
    from app.routers.loans import _proven_note_rate

    class _Loan:
        note_rate_pct = 7.99
        schedule_version = "B1"

    rate, proven = _proven_note_rate(_Loan())
    assert rate == 7.99
    assert proven is True


def test_the_list_item_no_longer_aliases_apr_unconditionally():
    """The schema itself must not relabel the column.

    Asserted on the model rather than only through a route, because the alias
    was the defect: any caller building a LoanListItem would inherit it.
    """
    from app.schemas import LoanListItem

    field = LoanListItem.model_fields["note_rate_pct"]
    assert field.validation_alias is None, (
        "note_rate_pct still aliases the raw apr column, so an unproven rate "
        "would be relabelled as contractual"
    )
    item = LoanListItem(id=1, principal=1000.0, term_months=12)
    assert item.note_rate_pct is None and item.note_rate_proven is False

def test_the_orm_no_longer_maps_the_dropped_column():
    """The other half of the contract step, and the one that breaks loudly.

    SQLAlchemy names every mapped column in its SELECT, so a lingering `pan`
    mapping would make every payment query fail the moment 0031 commits -- an
    outage, not a display bug. `_display_last4` not reading the attribute is not
    enough on its own; the column must be off the model.
    """
    from app import models

    mapped = set(models.Payment.__table__.columns.keys())
    assert "pan" not in mapped, "payments.pan is still mapped; 0031 would break every query"
    assert "cvv" not in mapped, "payments.cvv is still mapped; 0031 would break every query"
    assert "last4" in mapped
