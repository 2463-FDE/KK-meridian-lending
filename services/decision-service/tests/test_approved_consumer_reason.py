"""A model reason code must not become consumer wording by default.

Spec 0003 §1.1, §1.4, §1.6. The defect this closes was live and invisible:
`graph.py::_node_finalize` set `adverse_action_reason` to `reason_codes[0]`
unchanged, and that field is rendered to the applicant by
`frontend/app/apply/page.tsx`. The deterministic stub hides it completely,
because the stub's codes ARE full sentences -- so every test and every demo
looked correct while a real vendor returning `high_debt_to_income` would have
put that token in front of a declined person.

12 CFR 1002.9 requires a statement of *specific* reasons. A snake_case machine
token is not one.

No live scorer calls anywhere here: the vendor is a fake response object, which
is also the client brief's explicit instruction.
"""
import pytest

from app import decision, graph


# --------------------------------------------------------------------------
# The deterministic stub's two drivers.
#
# These map to themselves, and that is not a placeholder: their wording is
# owned by `_reason_codes` in this repository, which is exactly what makes them
# safe to show. Two entries is not a defect where two drivers is what the stub
# has.
# --------------------------------------------------------------------------

def test_the_low_bureau_driver_yields_an_approved_sentence():
    codes = decision._reason_codes(bureau_score=500, income=60_000)

    reason = decision.consumer_adverse_action_reason(codes, "deny")

    assert reason == decision.REASON_LOW_BUREAU_SCORE
    assert reason.endswith("lending criteria"), "not a sentence a person can read"
    assert "_" not in reason, "a machine token reached the consumer wording"


def test_the_insufficient_income_driver_yields_an_approved_sentence():
    codes = decision._reason_codes(bureau_score=800, income=0)

    reason = decision.consumer_adverse_action_reason(codes, "deny")

    assert reason == decision.REASON_INSUFFICIENT_INCOME
    assert "_" not in reason


@pytest.mark.parametrize("outcome", ["approve", "refer"])
def test_no_adverse_action_reason_for_a_non_denial(outcome):
    """An approval has no adverse action to explain."""
    assert decision.consumer_adverse_action_reason(
        [decision.REASON_LOW_BUREAU_SCORE], outcome) is None


# --------------------------------------------------------------------------
# Real vendor codes: unmapped, and therefore refused.
# --------------------------------------------------------------------------

def test_an_unmapped_vendor_code_fails_closed():
    with pytest.raises(decision.UnmappedAdverseActionReason):
        decision.consumer_adverse_action_reason(["high_debt_to_income"], "deny")


def test_the_refusal_does_not_quote_the_unmapped_code():
    """The code is model output and the reason we refuse it is that it is not
    fit to repeat onward. Interpolating it into the exception message would
    push it into logs, which is the same mistake one layer down."""
    with pytest.raises(decision.UnmappedAdverseActionReason) as exc:
        decision.consumer_adverse_action_reason(["high_debt_to_income"], "deny")

    assert "high_debt_to_income" not in str(exc.value)
    assert "1 reason code" in str(exc.value), "the operator gets no scale at all"


def test_a_denial_with_no_reason_codes_fails_closed():
    """An unexplained adverse action is the Reg B defect itself."""
    for empty in ([], None, [""], ["   "]):
        with pytest.raises(decision.UnmappedAdverseActionReason):
            decision.consumer_adverse_action_reason(empty, "deny")


def test_there_is_no_generic_fallback():
    """`GENERIC_REASONS` was removed once. Nothing may reintroduce a
    catch-all string for a code we cannot explain."""
    with pytest.raises(decision.UnmappedAdverseActionReason):
        decision.consumer_adverse_action_reason(["something_new"], "deny")


def test_there_is_no_nearest_match():
    """A code that merely resembles an approved one must not borrow its
    wording -- that would attribute a reason the model did not give."""
    nearly = decision.REASON_LOW_BUREAU_SCORE.lower()

    with pytest.raises(decision.UnmappedAdverseActionReason):
        decision.consumer_adverse_action_reason([nearly], "deny")


def test_the_mapping_table_contains_no_invented_vendor_entries():
    """The taxonomy is VENDOR-BLOCKED. Every entry must be a code this
    repository itself produces, so nothing here can be a guess."""
    ours = {decision.REASON_LOW_BUREAU_SCORE, decision.REASON_INSUFFICIENT_INCOME}

    assert set(decision.APPROVED_CONSUMER_REASONS) == ours, (
        "an entry was added for a code this repository does not itself emit; "
        "if it came from a vendor, the taxonomy is still not committed")
    assert "high_debt_to_income" not in decision.APPROVED_CONSUMER_REASONS, (
        "the test author's placeholder was promoted to an approved mapping")


# --------------------------------------------------------------------------
# Through the real graph, because the seam is only useful where it is called.
# --------------------------------------------------------------------------

def _result(codes, outcome="deny"):
    return {
        "score": 540.0, "decision": outcome, "reason_codes": codes,
        "model_version": "test-model-1", "top_features": None,
    }


def test_the_graph_publishes_the_approved_sentence_not_the_code():
    state = {"application": {}, "bureau_score": 500,
             "result": _result([decision.REASON_LOW_BUREAU_SCORE])}

    final = graph._node_finalize(state)["final"]

    assert final["adverse_action_reason"] == decision.REASON_LOW_BUREAU_SCORE
    # The code itself still travels, because it is the audit evidence.
    assert final["reason_codes"] == [decision.REASON_LOW_BUREAU_SCORE]


def test_the_graph_refuses_an_unmapped_vendor_code():
    state = {"application": {}, "bureau_score": 500,
             "result": _result(["high_debt_to_income"])}

    with pytest.raises(decision.UnmappedAdverseActionReason):
        graph._node_finalize(state)


def test_provenance_survives_the_mapping():
    """The audit record answers "what did the model say"; the notice answers
    "what is the applicant told". Mapping must not overwrite the first."""
    state = {"application": {}, "bureau_score": 800,
             "result": _result([decision.REASON_INSUFFICIENT_INCOME])}

    final = graph._node_finalize(state)["final"]

    assert final["reason_codes"] == [decision.REASON_INSUFFICIENT_INCOME]
    assert final["model_version"] == "test-model-1"


def test_an_approval_carries_no_reason_through_the_graph():
    state = {"application": {}, "bureau_score": 780,
             "result": _result([], outcome="approve")}

    final = graph._node_finalize(state)["final"]

    assert final["adverse_action_reason"] is None


# --------------------------------------------------------------------------
# Atomicity: the refusal has to happen before anything is written.
# --------------------------------------------------------------------------

def test_the_refusal_happens_before_any_persistence_can_occur():
    """decision-service is compute-only: origination writes `decisions` and
    `decision_events` in one transaction, and only after this call returns.
    So refusing inside the graph is what makes "no partial committed state"
    true -- there is nothing to roll back because nothing was written.

    Asserted structurally rather than with a database, because the property is
    architectural: this service must contain no write of its own.
    """
    import pathlib

    app_dir = pathlib.Path(decision.__file__).parent
    for module in app_dir.glob("*.py"):
        source = module.read_text(encoding="utf-8")
        for statement in ("INSERT INTO decisions", "INSERT INTO decision_events"):
            assert statement not in source, (
                f"{module.name} writes {statement!r}; a refusal after that "
                f"write would leave partial committed state")


def test_the_unmapped_refusal_is_a_model_unavailable_failure():
    """Subclassing means it inherits the existing fail-closed handling instead
    of needing a second one, while staying distinguishable in logs."""
    assert issubclass(decision.UnmappedAdverseActionReason,
                      decision.ModelUnavailableError)
    assert decision.UnmappedAdverseActionReason is not decision.ModelUnavailableError
