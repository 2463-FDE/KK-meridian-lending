"""The client's 28 acceptance cases are the authority, and all 28 are accounted for.

They shipped cases, not a design. That distinction is the useful one: a case says
what must happen and leaves how open, so running them proves the rule this
repository implemented is the rule they asked for rather than a rule it found
convenient.

**Every case is either resolved here or delegated to a named test.** The
delegation is the part worth distrusting — it is where "covered" can quietly mean
"assumed" — so this file asserts that each delegation target exists on disk, and
that handled plus delegated covers all 28 with nothing falling between.

**No case is skipped for being inconvenient.** If the resolver cannot answer a
case, that is a failure, not a category to add to the delegation table.
"""
import json
import pathlib
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
TOOLS = REPO / "db" / "tools"
sys.path.insert(0, str(TOOLS))

import governance_acceptance as gov  # noqa: E402
from client_governance_package import (  # noqa: E402
    PACKAGE_DIR,
    load_acceptance_evaluations,
    load_taxonomy,
    load_wording,
)

EXPECTED_CASE_COUNT = 28


@pytest.fixture(scope="module")
def report():
    return gov.run_acceptance()


def test_the_client_shipped_the_cases_their_readme_counts():
    cases = load_acceptance_evaluations()
    assert len(cases) == EXPECTED_CASE_COUNT
    readme = (PACKAGE_DIR / "evaluations" / "README.md").read_text(encoding="utf-8")
    assert str(EXPECTED_CASE_COUNT) in readme


def test_every_acceptance_case_passes(report):
    assert report["failed"] == [], (
        "client acceptance cases failed:\n  " + "\n  ".join(
            f"{f['eval_id']} ({f['category']}): {f['detail']}" for f in report["failed"]))


def test_every_case_is_accounted_for(report):
    assert report["total"] == EXPECTED_CASE_COUNT
    assert report["passed"] + report["delegated"] == EXPECTED_CASE_COUNT, (
        "a case is neither resolved here nor delegated, which means it is "
        "silently uncovered")


def test_every_delegated_case_names_a_test_that_exists(report):
    """A delegation to a file that does not exist is worse than no delegation."""
    missing = []
    for row in report["results"]:
        if row["status"] != "delegated":
            continue
        target = REPO / row["owner"]
        if not target.is_file():
            missing.append(f"{row['eval_id']} -> {row['owner']}")

    assert missing == [], (
        "acceptance cases are delegated to tests that do not exist:\n  "
        + "\n  ".join(missing))


def test_the_delegation_table_covers_only_containment_categories():
    """Delegation is for repository properties, not for hard resolver cases.

    Without this, the table becomes the place a failing case goes to stop
    failing.
    """
    allowed = {
        "synthetic_label_isolation", "proxy_prohibition", "unauthorized_role",
        "sensitive_data_retention", "prompt_injection", "fairness_overclaim",
    }
    assert set(gov._DELEGATED) == allowed, (
        "the delegation table changed; a resolver behaviour must not be moved "
        "into it to avoid implementing the case")


# --- the rules themselves, asserted directly ------------------------------

def test_an_unknown_code_never_reaches_consumer_wording():
    """EVAL-08, and the placeholder the repository has always refused.

    `high_debt_to_income` is a test author's placeholder that appears in this
    repository and in the client's own negative fixture. The client shipped it
    as an unknown code to be refused, which confirms rather than changes the
    repository's existing position: it must never be promoted into a taxonomy.
    """
    with pytest.raises(gov.ReasonRefused) as exc:
        gov.resolve(["high_debt_to_income"], "deny")

    assert "unmapped_code_passthrough" in exc.value.refusals
    assert "high_debt_to_income" not in str(exc.value), (
        "the unknown code was echoed in the refusal; it is vendor output and "
        "the point of refusing is that it is unfit to repeat")


def test_the_placeholder_is_not_in_the_client_taxonomy():
    assert "high_debt_to_income" not in load_taxonomy()


def test_a_mapped_code_returns_the_clients_exact_sentence():
    """EVAL-01. Exactly — not a paraphrase, not a nearest match."""
    got = gov.resolve(["CCUS-INC-AMT"], "deny")
    assert got["consumer_wording"] == "Income is insufficient for the amount of credit requested."
    assert got["approved_wording_id"] == "W-INC-AMT"
    assert got["raw_code_retained"] == "CCUS-INC-AMT"


def test_the_raw_code_survives_the_mapping():
    """Their boundary policy: mapping must not erase the raw code from audit
    evidence. The two artefacts travel together or the audit trail is broken."""
    for code in load_taxonomy():
        got = gov.resolve([code], "deny")
        assert got["raw_code_retained"] == code


def test_every_taxonomy_code_maps_to_wording_that_is_not_generic():
    """Guards the client's tables against each other.

    If a future package version added a code whose approved sentence was
    score-only, the mapping would produce insufficient wording while every
    individual file still looked correct.
    """
    wording = load_wording()
    for code, entry in load_taxonomy().items():
        approved = wording.get(entry["approved_wording_id"])
        assert approved is not None, f"{code} names wording that does not exist"
        assert not gov._is_generic(approved["plain_language_wording"]), (
            f"{code} maps to generic or score-only wording: "
            f"{approved['plain_language_wording']!r}")


def test_an_approval_gets_no_adverse_action_reason():
    assert gov.resolve(["CCUS-INC-AMT"], "approve") is None


@pytest.mark.parametrize("sentence", [
    "Model score too low.",
    "Internal policy.",
    "Applicant failed to achieve a qualifying score on the credit scoring system.",
    "The decision was based on internal standards.",
])
def test_generic_wording_is_refused_however_it_is_phrased(sentence):
    with pytest.raises(gov.ReasonRefused) as exc:
        gov.check_proposed_wording(sentence)
    assert set(gov._GENERIC_REFUSALS) <= set(exc.value.refusals)


def test_a_post_hoc_reason_is_refused_even_though_it_is_approved_wording():
    """EVAL-26, and the subtlest case they shipped.

    The proposed sentence is real approved wording — W-INC-AMT, verbatim. What
    makes it a violation is that the scorer emitted CCUS-BUR-DLQ, so the sentence
    describes a factor that was not the one scored. A check that only validated
    wording against the approved table would pass this.
    """
    with pytest.raises(gov.ReasonRefused) as exc:
        gov.check_proposed_wording(
            "Income is insufficient for the amount of credit requested.",
            scorer_emitted_codes=["CCUS-BUR-DLQ"])

    assert "post_hoc_reason_not_emitted_by_scorer" in exc.value.refusals


def test_the_wording_that_does_match_the_emitted_code_is_allowed():
    """The same check must not refuse everything, or it proves nothing."""
    emitted = ["CCUS-BUR-DLQ"]
    correct = gov.resolve(emitted, "deny")["consumer_wording"]
    assert gov.check_proposed_wording(correct, scorer_emitted_codes=emitted) == correct


def test_two_current_documents_stop_rather_than_pick_one():
    """EVAL-13. Sorting by date here would be the plausible wrong answer."""
    with pytest.raises(gov.ReasonRefused) as exc:
        gov.check_document_versions([
            {"version": "CCUS-SYN-2026.08.24", "current": True},
            {"version": "CCUS-SYN-2026.07.01", "current": True},
        ])
    assert "silent_pick_of_newer_or_older" in exc.value.refusals


def test_a_stale_document_is_refused():
    with pytest.raises(gov.ReasonRefused):
        gov.check_document_is_current("CCUS-SYN-2025.01.01", "CCUS-SYN-2026.08.24")


def test_an_unsupported_vendor_claim_is_rejected():
    with pytest.raises(gov.ReasonRefused) as exc:
        gov.check_vendor_claim("This synthetic card is production validated.")
    assert "production_validation_claim" in exc.value.refusals


def test_a_fairness_overclaim_is_rejected():
    """EVAL-16, at the claim-checking seam as well as in the evaluator."""
    with pytest.raises(gov.ReasonRefused):
        gov.check_vendor_claim("The model is fair based on the 32-row fixture.")


@pytest.mark.parametrize("bad_output", ["reason_codes not a list", None, {"a": 1}, 7])
def test_malformed_vendor_output_fails_closed(bad_output):
    with pytest.raises(gov.ReasonRefused) as exc:
        gov.check_vendor_output(bad_output)
    assert "stub_reason_substituted_for_vendor_failure" in exc.value.refusals


def test_an_unavailable_scorer_has_no_fallback():
    with pytest.raises(gov.ReasonRefused) as exc:
        gov.check_vendor_available("unavailable")
    assert "silent_stub_in_non_training_path" in exc.value.refusals


def test_every_negative_fixture_a_case_names_actually_resolves():
    """Their evaluations README requires it, and a broken path would mean a case
    referencing evidence nobody can open."""
    missing = []
    for case in load_acceptance_evaluations():
        rel = case["required_inputs"].get("negative_fixture")
        if rel and not (PACKAGE_DIR / rel).is_file():
            missing.append(f"{case['eval_id']} -> {rel}")

    assert missing == [], "cases name negative fixtures that do not exist:\n  " + \
        "\n  ".join(missing)


def test_negative_fixtures_stay_inside_their_own_folder():
    """Their README: negative fixtures are never approved inputs, and must not
    be copied into `vendor/`, into the fairness fixture, or into runtime.

    Containment by content, not by filename — a copy under a different name is
    the failure mode a name check would miss. An earlier version of this test
    asserted something stronger and wrong: that no code inside a negative
    fixture may exist in the taxonomy. `invented-post-hoc-reason.json` disproves
    it, and disproves it usefully. Its `scorer_emitted_codes` is `CCUS-BUR-DLQ`,
    a perfectly valid approved code. What makes that case a violation is the
    *wording* proposed alongside it, not the code — which is exactly why the
    client shipped it, and why a code-presence check is the wrong instrument.
    """
    import hashlib

    negative = {}
    for path in sorted((PACKAGE_DIR / "evaluations" / "fixtures").glob("*.json")):
        negative[hashlib.sha256(path.read_bytes()).hexdigest()] = path.name

    leaked = []
    for other in sorted(PACKAGE_DIR.rglob("*")):
        if not other.is_file() or "evaluations" in other.parts:
            continue
        digest = hashlib.sha256(other.read_bytes()).hexdigest()
        if digest in negative:
            leaked.append(f"{other.relative_to(PACKAGE_DIR)} == {negative[digest]}")

    assert leaked == [], (
        "a negative fixture has been copied outside evaluations/:\n  "
        + "\n  ".join(leaked))


def test_the_unknown_code_the_client_flags_is_not_in_the_taxonomy():
    """EVAL-08's second pass criterion, stated as its own check.

    The narrow, correct version of what the test above used to over-claim: the
    specific placeholder the client marks as unknown must not have been promoted
    into the approved vocabulary.
    """
    unknown = json.loads(
        (PACKAGE_DIR / "evaluations" / "fixtures" / "unknown-reason-code.json")
        .read_text(encoding="utf-8"))
    taxonomy = load_taxonomy()

    for code in _codes_in(unknown):
        assert code not in taxonomy, (
            f"{code!r} is flagged by the client as an unknown code and has been "
            f"promoted into the approved taxonomy")


def _codes_in(payload):
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in ("reason_codes", "scorer_emitted_codes") and isinstance(value, list):
                yield from (v for v in value if isinstance(v, str))
            else:
                yield from _codes_in(value)
    elif isinstance(payload, list):
        for item in payload:
            yield from _codes_in(item)
