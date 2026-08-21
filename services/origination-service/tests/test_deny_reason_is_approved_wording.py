"""`get_deny_reason` must not hand back the model's own reason code.

Spec 0003 §1.1. This function feeds the 422 detail on the boarding route
(`applications.py`) and the offer-creation route (`offers.py`), so whatever it
returns is rendered to whoever called those. It used to return
`reason_codes[0]` unchanged, which is fine while the deterministic stub emits
full sentences and is a raw machine token the moment a real vendor answers.

The mapping table here is duplicated from decision-service because the two
services do not import one another; `db/tests/test_approved_reasons_agree_across_services.py`
holds the two copies together. That test compares the tables. This one
exercises the function, which is a different question — a correct table is no
use if the caller bypasses it, and a mutation that made `get_deny_reason`
return the raw code again passed the table test untouched.
"""
import pytest

from app import db, decision_state

APPROVED = "Low credit bureau score relative to lending criteria"


@pytest.fixture(autouse=True)
def _no_manual_review(monkeypatch):
    """These cases are about the automated path."""
    monkeypatch.setattr(decision_state, "get_manual_review", lambda app_id: None)


def _stub_events(monkeypatch, reason_codes):
    monkeypatch.setattr(db, "query", lambda *a, **k: [{"reason_codes": reason_codes}])


def test_an_approved_code_is_returned_as_its_approved_wording(monkeypatch):
    _stub_events(monkeypatch, [APPROVED])

    assert decision_state.get_deny_reason(4242) == APPROVED


def test_an_unmapped_vendor_code_is_not_returned(monkeypatch):
    """Returns None so the caller renders its existing "not on record" wording.

    Deliberately not a substitute reason: these are operational messages, and
    declining to state a reason is honest where inventing one is the
    `GENERIC_REASONS` defect this repository already removed.
    """
    _stub_events(monkeypatch, ["high_debt_to_income"])

    assert decision_state.get_deny_reason(4242) is None


def test_the_raw_code_never_leaks_through_this_function(monkeypatch):
    for token in ("high_debt_to_income", "DEROGATORY_HISTORY", "score_band_4"):
        _stub_events(monkeypatch, [token])

        result = decision_state.get_deny_reason(4242)

        assert result != token
        assert result is None or "_" not in result


def test_no_decision_event_yields_none(monkeypatch):
    monkeypatch.setattr(db, "query", lambda *a, **k: [])

    assert decision_state.get_deny_reason(4242) is None


def test_a_staff_reason_is_returned_untouched(monkeypatch):
    """A person wrote that sentence and owns it, so it does not go through the
    model's mapping table -- which would reject it and silently drop a reason a
    human is accountable for."""
    monkeypatch.setattr(
        decision_state, "get_manual_review",
        lambda app_id: {"outcome": "deny", "reason": "Applicant withdrew documentation"})

    assert decision_state.get_deny_reason(4242) == "Applicant withdrew documentation"


def test_the_approved_table_holds_only_readable_sentences():
    for code, wording in decision_state.APPROVED_CONSUMER_REASONS.items():
        assert "_" not in wording, f"{code!r} maps to a machine token"
        assert " " in wording.strip()
