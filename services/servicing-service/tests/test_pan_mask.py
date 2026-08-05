"""Payment-history display tests (these PASS).

ADR 0008 (Week 5 tokenization): new payments carry `last4` directly (from the
processor's own token response, never a raw PAN) -- displayed as-is. Legacy
rows that predate tokenization still have a full `pan`; those are still
masked the same way this always displayed, so old payment history doesn't
regress.
"""
from types import SimpleNamespace

from app.routers.loans import _display_last4


def test_prefers_stored_last4_for_a_tokenized_row():
    payment = SimpleNamespace(last4="1111", pan=None)
    assert _display_last4(payment) == "•••• 1111"


def test_falls_back_to_masking_a_legacy_full_pan():
    payment = SimpleNamespace(last4=None, pan="4111111111111111")
    assert _display_last4(payment) == "•••• 1111"


def test_prefers_last4_over_a_legacy_pan_if_somehow_both_are_set():
    payment = SimpleNamespace(last4="9999", pan="4111111111111111")
    assert _display_last4(payment) == "•••• 9999"


def test_handles_neither_present():
    payment = SimpleNamespace(last4=None, pan=None)
    assert _display_last4(payment) is None
