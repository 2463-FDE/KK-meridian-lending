"""Payment-history display tests.

ADR 0008 (Week 5 tokenization) stopped this system receiving card numbers at
all: a payment carries `last4` from the processor's token response, never a
PAN. `db/migrations/0029` then dropped the legacy `pan`/`cvv` columns outright,
back-filling `last4` from `pan` first so historical payments still display.

That back-fill is why the old "fall back to masking a full pan" case is gone
rather than merely deleted: after 0029 there is no column to fall back to, and
no code path in this service can reach a full card number. The tests below pin
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
    """Regression guard for the 0029 drop. If a `pan` fallback is ever
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
