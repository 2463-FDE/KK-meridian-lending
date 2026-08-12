"""A model risk label must not reach staff through prose either.

Removing `risk_tier` from the response contract closed the STRUCTURED path --
the coloured chip. It did not close the prose path. A response of

    {"summary": "High-risk borrower...", "flags": ["High risk"]}

validates against `_LLMOutput`, survives `model_dump()`, and renders in the
staff UI exactly where the chip used to be. The invariant this PR claims is
that no unaudited model risk label reaches staff; before this guard existed it
held for one of the two ways a label can arrive.

The system prompt already forbids it, and that is not enough. This file already
says so about a different claim: `_strip_contradicting_macro_claims` exists
because a prompt can discourage but not prevent. Same class of failure, so the
same shape of guard -- and the same shape of test.

These feed the model's output in directly rather than mocking at the HTTP layer,
because what is under test is what happens to a response AFTER it parses.
"""
import pytest

from app import llm_client
from app.llm_client import (
    LLMResponseError,
    _is_a_risk_classification,
    _strip_risk_classifications,
)

# Every way the label has actually shown up, plus the ones a prompt change would
# invite next. Each must not survive to staff.
CLASSIFICATIONS = [
    "High-risk borrower with unstable income.",
    "This is a low risk application.",
    "Risk tier: high.",
    "Overall risk rating is moderate.",
    "Risk grade: B.",
    "I would classify this applicant as elevated risk.",
    "Recommend to decline.",
    "This application should be denied.",
    "The borrower presents significant risk.",
    # Review round 3. Every one of these reached the staff payload under the
    # first version of the guard, which matched adjective-then-risk and active
    # recommendations only. The adjective moving to the other side of the verb
    # was enough to defeat it.
    "The application risk is high.",
    "The overall risk appears moderate.",
    "This borrower is a poor credit risk.",
    "Decline is recommended.",
    "Approval is not recommended.",
    "Credit risk is unacceptable.",
    "The loan should not be approved.",
]

# Descriptive, grounded observations. These are the whole point of the summary
# and must survive -- a guard that also removed these would have replaced a
# compliance defect with a useless product.
DESCRIPTIVE = [
    "Employment history reduces repayment risk.",
    "Loan amount is large relative to stated income.",
    "Employment under one year.",
    "The applicant reports six years at the same employer.",
    "Stated income is $71,000 against a $24,000 request.",
    "There is a risk the stated employer could not be verified.",
    "The applicant asked about refinancing risk disclosures.",
    "Payment history shows no late payments.",
]


@pytest.mark.parametrize("text", CLASSIFICATIONS)
def test_a_classification_is_recognised(text):
    assert _is_a_risk_classification(text), f"not caught: {text!r}"


@pytest.mark.parametrize("text", DESCRIPTIVE)
def test_a_descriptive_observation_is_not_touched(text):
    assert not _is_a_risk_classification(text), (
        f"false positive on {text!r} -- this is the kind of grounded sentence "
        "the summary exists to produce, and removing it makes the guard worse "
        "than the defect."
    )


def test_a_risk_label_in_the_summary_is_removed():
    summary = (
        "High-risk borrower with unstable income. "
        "The applicant reports six years at the same employer. "
        "Stated income is $71,000 against a $24,000 request."
    )
    cleaned, flags, dropped = _strip_risk_classifications(summary, [])
    assert dropped == 1
    assert "high-risk" not in cleaned.lower()
    assert "six years at the same employer" in cleaned, (
        "the guard removed the grounded prose as well"
    )


def test_a_risk_label_in_the_flags_is_removed():
    flags = ["High risk", "Employment under one year", "Risk tier: high"]
    _, kept, dropped = _strip_risk_classifications("Some grounded prose here.", flags)
    assert kept == ["Employment under one year"]
    assert dropped == 2


def test_a_summary_that_is_only_classification_fails_closed():
    """Mirrors the macro scrub: if nothing survives, there is no summary to show,
    and inventing one here would make this service the author."""
    with pytest.raises(LLMResponseError):
        _strip_risk_classifications("High-risk borrower. Recommend to decline.", [])


def test_the_guard_runs_in_summarize_application(monkeypatch):
    """The end-to-end assertion: nothing reaches the returned payload.

    The unit tests above prove the function works. This proves it is WIRED --
    a guard that is never called is the defect with a test suite attached.
    """
    import json

    payload = {
        "loan_amount": 24000.0,
        "term_months": 48,
        "purpose": "debt consolidation",
        "summary": (
            "High-risk borrower. The applicant reports six years at the same "
            "employer."
        ),
        "flags": ["High risk", "Employment under one year"],
    }

    monkeypatch.setattr(llm_client, "make_client", lambda: object())
    monkeypatch.setattr(llm_client, "call_api", lambda client, prompt: json.dumps(payload))
    monkeypatch.setattr(llm_client, "_fetch_signal", lambda *a, **k: None, raising=False)

    result = llm_client.summarize_application({
        "id": 1,
        "applicant": {"name": "Robin Fictional"},
        "amount": 24000, "term_months": 48, "purpose": "debt consolidation",
        "income": 71000, "employment_years": 6,
    })

    blob = f"{result.summary} {' '.join(result.flags)}".lower()
    assert "high-risk" not in blob and "high risk" not in blob, (
        f"a model risk label reached the staff payload: {blob!r}"
    )
    assert "six years at the same employer" in result.summary, (
        "the grounded sentence was removed along with the label"
    )
    assert "Employment under one year" in result.flags


def test_the_parametrized_guards_are_not_vacuous():
    assert len(CLASSIFICATIONS) >= 5 and len(DESCRIPTIVE) >= 5
