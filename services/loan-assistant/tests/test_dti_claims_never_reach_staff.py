"""A model-authored debt-to-income claim must not reach staff.

G-DTI removed the published DTI cutoff from the policy and the instruction from
the prompt. Neither stops the model writing one anyway, and a prompt cannot --
the same reason `_strip_contradicting_macro_claims` and
`_strip_risk_classifications` exist in this file.

A response of `flags: ["Debt-to-income near the policy limit"]` validated against
`_LLMOutput`, survived both existing scrubs, and was returned to the officer. So
the criterion this branch retired was back in front of staff during manual
review, and it is worse than a stale document: nothing in this system carries the
applicant's existing debt obligations (adr/0007), so the ratio was computed from
data the model was never given.

**The constraint that makes this non-trivial is `debt consolidation`.** It is a
real and common loan purpose that the summary SHOULD discuss, and it contains the
word "debt". A guard that blocked it would break the product to fix a compliance
defect. So both directions are asserted here, and the keep-list is as long as the
block-list on purpose.
"""
import json

import pytest

from app import llm_client
from app.llm_client import LLMResponseError, _is_a_dti_claim, _strip_dti_claims

# Fabricated ratios, in the phrasings a model actually reaches for.
DTI_CLAIMS = [
    "Debt-to-income near the policy limit.",
    "DTI is approximately 38%.",
    "The applicant's debt to income ratio is acceptable.",
    "D.T.I. exceeds guidance.",
    "Existing obligations are high relative to income.",
    "Monthly debt as a percentage of income is elevated.",
    "The income to debt relationship is weak.",
    "Their debt ratio suggests strain.",
    "Debt/income sits just under the threshold.",
    "Outstanding liabilities compared with income are significant.",
]

# Legitimate prose. Four of these mention debt, because `debt consolidation` is
# a loan purpose and the officer summary is supposed to name it.
LEGITIMATE = [
    "The stated purpose is debt consolidation.",
    "Debt consolidation may simplify the applicant's monthly payments.",
    "The applicant is consolidating credit card debt.",
    "A debt consolidation loan of $24,000 over 48 months.",
    # The system prompt's own example of a good flag. It relates the LOAN AMOUNT
    # to income -- two figures the model is actually given -- and is not a DTI.
    "Loan amount is large relative to stated income.",
    "Stated income is $71,000 against a $24,000 request.",
    "Employment under one year.",
    "Payment history shows no late payments.",
    # Review round 2 on this guard. `payment` was in the obligations pattern, so
    # these were scrubbed -- statements about the REQUESTED loan, computed from
    # the amount, term and income the model is given, and explicitly invited by
    # the system prompt. A one-sentence summary of this shape failed the whole
    # request closed, which is worse than the defect: the officer loses real
    # repayment-capacity context and the borrower's file will not render.
    "The estimated monthly payment is manageable relative to stated income.",
    "The monthly payment is small compared to income.",
    "The requested payment is modest against the stated income.",
]


@pytest.mark.parametrize("text", DTI_CLAIMS)
def test_a_dti_claim_is_recognised(text):
    assert _is_a_dti_claim(text), f"not caught: {text!r}"


@pytest.mark.parametrize("text", LEGITIMATE)
def test_legitimate_debt_prose_is_not_touched(text):
    assert not _is_a_dti_claim(text), (
        f"false positive on {text!r}. `debt consolidation` is a real loan "
        "purpose and amount-to-income is a grounded observation; blocking "
        "either breaks the summary to fix a compliance defect."
    )


def test_a_dti_sentence_is_removed_and_the_rest_survives():
    summary = (
        "The stated purpose is debt consolidation. "
        "Debt-to-income is near the policy limit. "
        "The applicant reports six years at the same employer."
    )
    cleaned, _, dropped = _strip_dti_claims(summary, [])
    assert dropped == 1
    assert "debt-to-income" not in cleaned.lower()
    assert "debt consolidation" in cleaned, (
        "the legitimate purpose sentence was removed with the ratio"
    )
    assert "six years at the same employer" in cleaned


def test_a_dti_flag_is_removed_and_legitimate_flags_survive():
    flags = [
        "Debt-to-income near the policy limit",
        "Debt consolidation request",
        "Loan amount is large relative to stated income",
    ]
    _, kept, dropped = _strip_dti_claims("Grounded prose here.", flags)
    assert kept == ["Debt consolidation request",
                    "Loan amount is large relative to stated income"]
    assert dropped == 1


def test_a_summary_that_is_only_dti_fails_closed():
    """Mirrors the two scrubs beside it: if nothing survives there is no summary
    to show, and inventing one would make this service the author."""
    with pytest.raises(LLMResponseError):
        _strip_dti_claims("DTI is 38%. Debt-to-income exceeds guidance.", [])


def test_the_guard_runs_in_summarize_application(monkeypatch):
    """The end-to-end assertion, because a guard that is never called is the
    defect with a test suite attached."""
    payload = {
        "loan_amount": 24000.0,
        "term_months": 48,
        "purpose": "debt consolidation",
        "summary": (
            "The stated purpose is debt consolidation. "
            "Debt-to-income is near the policy limit."
        ),
        "flags": ["Debt-to-income near the policy limit", "Debt consolidation request"],
    }

    # The agent runtime is exercised by test_agent_*.py; these cases are
    # about what happens to the text afterwards.
    monkeypatch.setattr(llm_client, "_summary_text_via_agent",
                        lambda prompt: json.dumps(payload))
    monkeypatch.setattr(llm_client, "_fetch_signal", lambda *a, **k: None, raising=False)

    result = llm_client.summarize_application({
        "id": 1,
        "applicant": {"name": "Robin Fictional"},
        "amount": 24000, "term_months": 48, "purpose": "debt consolidation",
        "income": 71000, "employment_years": 6,
    })

    blob = f"{result.summary} {' '.join(result.flags)}".lower()
    assert "debt-to-income" not in blob and "dti" not in blob, (
        f"a fabricated DTI claim reached the staff payload: {blob!r}"
    )
    # ...and the product still works: the purpose is still described.
    assert "debt consolidation" in blob, (
        "the legitimate loan purpose was scrubbed along with the ratio"
    )


def test_no_threshold_or_new_rule_was_introduced():
    """G-DTI's whole point is that Meridian does not evaluate debt-to-income.

    A guard that compared against a number would be the defect this PR closed,
    reintroduced as code instead of prose -- an unapproved cutoff, now enforced.

    Checked on the parsed syntax tree rather than by searching the text. The
    first version of this test looked for the word "threshold" and tripped on
    its own docstring saying "No threshold is introduced" -- a keyword scan
    cannot tell a cutoff from a sentence denying there is one.
    """
    import ast
    import inspect

    for fn in (_strip_dti_claims, _is_a_dti_claim):
        tree = ast.parse(inspect.getsource(fn).lstrip())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Compare):
                continue
            operands = [node.left, *node.comparators]
            numeric = [
                n for n in operands
                if isinstance(n, ast.Constant) and isinstance(n.value, (int, float))
                and not isinstance(n.value, bool)
            ]
            assert not numeric, (
                f"{fn.__name__} compares against {[n.value for n in numeric]} -- "
                "the guard must remove DTI claims, not adjudicate them against a "
                "cutoff nobody approved. That would be G-DTI's defect "
                "reintroduced as code instead of prose."
            )


def test_the_parametrized_guards_are_not_vacuous():
    assert len(DTI_CLAIMS) >= 5 and len(LEGITIMATE) >= 5
    assert sum("debt" in t.lower() for t in LEGITIMATE) >= 4, (
        "the keep-list must contain real debt prose, or it proves nothing about "
        "the false-positive risk that makes this guard hard"
    )


def test_a_lone_repayment_capacity_sentence_does_not_fail_the_request(monkeypatch):
    """The fail-closed path, reached by a false positive, is the worst outcome.

    `_strip_dti_claims` raises when nothing survives. So a one-sentence summary
    that the guard wrongly matched did not merely lose a sentence -- it turned a
    good summary into an error and the officer saw no file at all. This asserts
    the whole pipeline on exactly that shape.
    """
    payload = {
        "loan_amount": 24000.0,
        "term_months": 48,
        "purpose": "debt consolidation",
        "summary": "The estimated monthly payment is manageable relative to stated income.",
        "flags": [],
    }

    # The agent runtime is exercised by test_agent_*.py; these cases are
    # about what happens to the text afterwards.
    monkeypatch.setattr(llm_client, "_summary_text_via_agent",
                        lambda prompt: json.dumps(payload))
    monkeypatch.setattr(llm_client, "_fetch_signal", lambda *a, **k: None, raising=False)

    result = llm_client.summarize_application({
        "id": 1, "applicant": {"name": "Robin Fictional"},
        "amount": 24000, "term_months": 48, "purpose": "debt consolidation",
        "income": 71000, "employment_years": 6,
    })
    assert "manageable relative to stated income" in result.summary


def test_a_payment_on_an_existing_debt_is_still_a_dti_claim():
    """Narrowing must not have opened the real hole.

    "monthly payments on existing debts relative to income" IS the fabricated
    ratio; it survives the narrowing via the debt/obligation alternation.
    """
    assert _is_a_dti_claim(
        "Monthly payments on existing debts are high relative to income.")
    assert _is_a_dti_claim(
        "Outstanding obligations as a percentage of income are elevated.")
