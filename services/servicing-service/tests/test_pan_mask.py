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


def test_a_pre_0029_row_still_displays_from_the_legacy_pan():
    """The deployment window automated review caught on PR #11.

    Nothing in this change enforces that db/migrations/0029 has back-filled
    `last4` before this service version serves traffic, and deploys are not
    atomic. So this row shape is real: `pan` populated, `last4` still NULL.

    Without the fallback the card column blanks on every historical payment --
    no error, just missing data, which is precisely what the back-fill exists to
    prevent. Only the last four digits are ever returned.
    """
    class _PreBackfillRow:
        last4 = None
        brand = "visa"
        pan = "4111111111111111"

    assert _display_last4(_PreBackfillRow()) == "•••• 1111"


def test_the_legacy_fallback_never_returns_more_than_four_digits():
    """The fallback reads a column that still holds a full PAN, so the slice is
    the control. A regression that returned the whole value would put a card
    number on screen."""
    class _PreBackfillRow:
        last4 = None
        brand = "amex"
        pan = "340000000000009"

    out = _display_last4(_PreBackfillRow())
    assert out == "•••• 0009"
    assert "340000000000009" not in out
    assert len(out.replace("•••• ", "")) == 4


def test_last4_wins_over_the_legacy_pan_once_the_backfill_has_run():
    """After 0029 both columns are populated. The back-filled `last4` is the
    supported source; the fallback must not shadow it."""
    class _PostBackfillRow:
        last4 = "4242"
        brand = "visa"
        pan = "4111111111111111"

    assert _display_last4(_PostBackfillRow()) == "•••• 4242"
# --- a legacy loan's APR is not a note rate ----------------------------------

def test_a_loan_without_a_stored_schedule_reports_no_note_rate():
    """`loans.apr` means different things depending on how the loan was boarded.

    The pre-change acceptance path copied `offers.apr` -- the DISCLOSED APR --
    into that column: 5.196% for a contract priced at 7.99%. An unconditional
    alias therefore printed 5.196% to those borrowers as "Interest rate (note
    rate)", a contractual term they were never quoted, while migration 0030
    refuses to trust the same column. Reviewed on PR #10.

    `schedule_version` is written only by the current boarding path, which also
    copies the contractual rate, so it is the evidence the value means what the
    API calls it.
    """
    from app.routers.loans import _proven_note_rate

    class _Loan:
        apr = 5.196
        schedule_version = None

    rate, proven = _proven_note_rate(_Loan())
    assert rate is None, "an unproven APR was reported as the contractual rate"
    assert proven is False


def test_a_loan_boarded_with_its_contract_reports_the_note_rate():
    """The other half: a proven rate must still be shown."""
    from app.routers.loans import _proven_note_rate

    class _Loan:
        apr = 7.99
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
