"""The prompt that is actually sent must not ask for a risk classification.

Every earlier guard on this checked a *representation* of the prompt -- the
response contract, the API schema, the frontend type, the fixtures. All of them
were clean while `_build_prompt()` itself still contained the literal line

    "... as JSON with fields: loan_amount, term_months, purpose, risk_tier, ..."

so the model was still being told to produce a tier. Because `_LLMOutput`
ignores unknown keys, whatever it returned was dropped without an error, a log
line or a failing test. Nothing reached staff, and the instruction to form an
unaudited judgment was still in the request -- which colours the prose and the
flags that DO reach staff.

So these tests call `_build_prompt()` and read the string it returns. That is
the only artifact the model ever sees.
"""
import re

import pytest

from app.llm_client import _LLMOutput, _build_prompt


def _prompt():
    return _build_prompt({
        "amount": 24000,
        "term_months": 48,
        "purpose": "debt consolidation",
        "income": 71000,
        "employer": "Fictional Freight Co",
        "job_title": "Dispatcher",
        "employment_years": 6,
    })


# Anything that asks the model to place this application on a scale. Substrings,
# because "risk_tier", "risk tier", "riskTier" and "risk-tier" are the same ask.
FORBIDDEN = (
    "risk_tier", "risk tier", "risktier", "risk-tier",
    "risk_rating", "risk rating", "risk_grade", "risk grade",
    "risk_score", "risk score", "risk_category", "risk category",
    "risk_class", "creditworthiness rating", "credit grade",
    "tier", "grade", "rating",
)


@pytest.mark.parametrize("term", FORBIDDEN)
def test_the_rendered_prompt_asks_for_no_risk_classification(term):
    assert term not in _prompt().lower(), (
        f"the prompt sent to the model contains {term!r}. There is no approved "
        "deterministic rule mapping an application to a tier, so asking for one "
        "produces an unaudited judgment that colours the summary staff read."
    )


def test_the_requested_fields_are_exactly_the_response_contract():
    """The fix, rather than today's symptom.

    `risk_tier` outliving its removal was possible only because the prompt kept
    its own hand-written copy of the field list. Asserting the two are the same
    statement catches the NEXT field to drift, in either direction -- a field
    added to the contract but never requested is equally broken, and reads as
    working because the model simply omits it.
    """
    line = _prompt().splitlines()[0]
    match = re.search(r"fields:\s*(.+?)\.\s*$", line)
    assert match, f"could not find the requested-field list in: {line!r}"

    requested = [f.strip() for f in match.group(1).split(",")]
    assert requested == list(_LLMOutput.model_fields), (
        "the prompt's field list and the response contract have diverged. "
        f"prompt={requested} contract={list(_LLMOutput.model_fields)}"
    )


def test_the_guard_is_not_vacuous():
    """A parametrized test over terms that could never appear proves nothing.

    If `_build_prompt` returned "" or a stub, every assertion above would pass.
    """
    prompt = _prompt()
    assert len(prompt) > 200, "the prompt is too short to be the real one"
    assert "Summarize this loan application as JSON" in prompt
    # The fields that SHOULD be requested really are, so the forbidden-term
    # assertions ran against a prompt that genuinely lists fields.
    for expected in ("loan_amount", "term_months", "purpose", "summary", "flags"):
        assert expected in prompt, f"{expected} is not requested at all"


def test_the_contract_itself_declares_no_risk_classification():
    """Belt and braces: the field list is derived, so a tier re-added to the
    contract would silently re-enter the prompt. This is what stops that."""
    for name in _LLMOutput.model_fields:
        lowered = name.lower()
        assert not any(t in lowered for t in ("tier", "grade", "rating", "risk")), (
            f"{name!r} is back in the response contract, and the prompt is "
            "derived from it -- so it is back in the prompt too."
        )
