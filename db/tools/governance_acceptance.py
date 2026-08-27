"""Resolve a reason code to approved consumer wording, or refuse — offline.

The client's package ships 28 acceptance cases that say what must happen for a
given reason code: which wording is produced, which inputs are refused, and what
escalates. This module implements the resolution rule their policies describe and
runs their cases against it, so the package is *used* rather than stored.

**This is not the runtime adverse-action path and must never become it.**
`services/decision-service/app/decision.py::consumer_adverse_action_reason` stays
exactly as it is, with `APPROVED_CONSUMER_REASONS` holding only the two reasons
the local stub scorer actually emits. Wiring the twelve `CCUS-*` codes into it
would be nearest-match substitution — mapping a stub's internal reason onto a
vendor taxonomy the stub does not emit — which the client's
`adverse-action-and-reason-code-boundary.md` prohibits by name, and which their
`vendor-document-precedence-and-versioning.md` forbids again by placing this
synthetic packet in the lowest tier with no vendor-issued document above it.

So this resolver answers a governance question — *would the mapping rule behave
correctly if a real taxonomy existed* — and answers it offline, against synthetic
codes, in a tool no service imports.

Usage:

    python db/tools/governance_acceptance.py
    python db/tools/governance_acceptance.py --json
"""
from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

from client_governance_package import (  # noqa: E402  (path set immediately above)
    PACKAGE_VERSION,
    TRAINING_BANNER,
    load_acceptance_evaluations,
    load_taxonomy,
    load_wording,
    require_intact,
)


class ReasonRefused(Exception):
    """No approved consumer wording may be produced for this input.

    Carries the refusal categories in the client's own vocabulary, so an
    acceptance case can assert the refusal was the one required rather than
    merely that something failed.
    """

    def __init__(self, message: str, refusals: tuple[str, ...], escalate: bool = True):
        super().__init__(message)
        self.refusals = refusals
        self.escalate = escalate


#: 12 CFR 1002.9's insufficient-statement classes, in the client's vocabulary.
#:
#: A generic sentence is refused under all three together rather than under
#: whichever one it most resembles. That is their design, not a shortcut: EVAL-09
#: supplies one sentence, "Model score too low.", and requires all three names in
#: the refusal, and its pass criterion is that the insufficient-statement
#: examples — plural — are treated as refusals.
_GENERIC_REFUSALS = (
    "generic_score_reason",
    "internal_policy_reason",
    "qualifying_score_reason",
)

#: Phrases that make a proposed consumer sentence insufficient. Taken from the
#: taxonomy's own `prohibited_generic_wording` field and the boundary policy.
_GENERIC_MARKERS = (
    "internal standard",
    "internal policy",
    "internal policies",
    "qualifying score",
    "score too low",
    "model score",
    "credit scoring system",
)


def _is_generic(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in _GENERIC_MARKERS)


def resolve(reason_codes, outcome, taxonomy=None, wording=None):
    """The approved sentence for a denial, or `ReasonRefused`.

    One-to-one through the client's tables, or nothing. Each branch is a rule
    from `adverse-action-and-reason-code-boundary.md`, in the order it states them.
    """
    taxonomy = taxonomy if taxonomy is not None else load_taxonomy()
    wording = wording if wording is not None else load_wording()

    if outcome != "deny":
        return None

    codes = [c for c in (reason_codes or []) if isinstance(c, str) and c.strip()]
    if not codes:
        raise ReasonRefused(
            "a denial was reported with no reason code",
            ("consumer_facing_reason", "generic_fallback"))

    code = codes[0]
    entry = taxonomy.get(code)
    if entry is None:
        # The unknown code is never echoed. It is vendor output, refusing it is
        # precisely a statement that it is unfit to repeat, and EVAL-08's pass
        # criterion is that it never reaches consumer wording.
        raise ReasonRefused(
            f"{len(codes)} reason code(s) reported, none present in taxonomy "
            f"{PACKAGE_VERSION}",
            ("unmapped_code_passthrough", "nearest_match", "generic_fallback",
             "reviewer_invented_reason"))

    if entry.get("version") != PACKAGE_VERSION:
        raise ReasonRefused(
            f"taxonomy entry is version {entry.get('version')!r}, not the "
            f"current {PACKAGE_VERSION!r}",
            ("use_of_stale_taxonomy",))

    approved = wording.get(entry["approved_wording_id"])
    if approved is None:
        raise ReasonRefused(
            "the taxonomy entry names approved wording that does not exist",
            ("unmapped_code_passthrough", "nearest_match"))

    sentence = approved["plain_language_wording"]
    if _is_generic(sentence):
        raise ReasonRefused("the approved wording reads as generic or score-only",
                            _GENERIC_REFUSALS)

    return {
        "consumer_wording": sentence,
        "approved_wording_id": approved["approved_wording_id"],
        # Retained deliberately: the boundary policy says mapping must not erase
        # the raw code from audit evidence.
        "raw_code_retained": code,
    }


def check_proposed_wording(sentence, scorer_emitted_codes=None,
                           taxonomy=None, wording=None):
    """Refuse a consumer sentence someone wants to send.

    A separate entry point because several client cases supply a *sentence*
    rather than a code — a reviewer, or an upstream, proposing text directly.
    That is the shape a post-hoc reason actually arrives in, so it needs its own
    door rather than being squeezed through the code path.
    """
    if _is_generic(sentence):
        raise ReasonRefused(
            "a generic, internal-policy or score-only reason is insufficient "
            "under 12 CFR 1002.9",
            _GENERIC_REFUSALS)

    if scorer_emitted_codes:
        allowed = resolve(scorer_emitted_codes, "deny", taxonomy, wording)
        if allowed and sentence.strip() != allowed["consumer_wording"]:
            raise ReasonRefused(
                "the proposed wording does not map from a code the scorer emitted",
                ("post_hoc_reason_not_emitted_by_scorer", "nearest_match"))
    return sentence


def check_document_versions(docs):
    """Two documents both marked current is a stop, not a tiebreak.

    Their precedence policy says so explicitly, and says the conflict is not
    resolved by picking one. So this raises rather than sorting by date, which is
    the tempting and wrong implementation.
    """
    current = [d for d in docs if d.get("current")]
    if len(current) > 1:
        raise ReasonRefused(
            f"{len(current)} vendor documents are both marked current",
            ("silent_pick_of_newer_or_older",))
    return True


def check_document_is_current(offered_version, current_version):
    if offered_version != current_version:
        raise ReasonRefused(
            f"offered document {offered_version!r} is not the current approved "
            f"{current_version!r}",
            ("use_of_stale_taxonomy",))
    return True


#: Claims this packet cannot support. Their README and precedence policy both
#: treat a vendor claiming production fairness or legal compliance as a stop.
_UNSUPPORTED_CLAIM_MARKERS = (
    "production validated", "production-validated", "legally compliant",
    "is fair", "production fairness",
)


def check_vendor_claim(claim):
    lowered = (claim or "").lower()
    if any(m in lowered for m in _UNSUPPORTED_CLAIM_MARKERS):
        raise ReasonRefused(
            "the vendor claim is not supported by this synthetic packet",
            ("production_validation_claim",
             "production_or_real_world_fairness_claim"))
    return True


def check_vendor_output(vendor_output):
    """Malformed scorer output fails closed.

    The refusal names cover the two tempting repairs their case calls out:
    inventing a reason after the failure, and substituting the local stub's
    reason for a vendor that did not answer.
    """
    if not isinstance(vendor_output, list):
        raise ReasonRefused(
            "vendor output is not a list of reason codes",
            ("invented_reason_after_failure",
             "stub_reason_substituted_for_vendor_failure"))
    return True


def check_vendor_available(status):
    if status != "available":
        raise ReasonRefused(
            f"the scorer is {status!r}; there is no approved fallback",
            ("silent_stub_in_non_training_path",
             "stub_reason_substituted_for_vendor_failure"))
    return True


#: Acceptance categories that are containment properties of the repository rather
#: than resolver behaviour. Each names the test that actually proves it, so a
#: reader can follow a case to its evidence instead of assuming it is covered.
#: `db/tests/test_governance_acceptance_evaluations.py` asserts every delegation
#: target exists on disk, and that handled ∪ delegated covers all 28 cases.
_DELEGATED = {
    "synthetic_label_isolation": "db/tests/test_no_runtime_protected_class_proxy.py",
    "proxy_prohibition": "db/tests/test_no_runtime_protected_class_proxy.py",
    "unauthorized_role": "db/tests/test_no_runtime_protected_class_proxy.py",
    "sensitive_data_retention":
        "services/payment-service/tests/test_pan_cvv_never_enter_the_payment_path.py",
    "prompt_injection": "services/loan-assistant/tests/test_prompt_injection.py",
    "fairness_overclaim": "db/tests/test_offline_fairness_eval.py",
}


def _run_case(case, taxonomy, wording):
    """Dispatch on the shape of `required_inputs`, which is what varies."""
    inputs = case["required_inputs"]

    if "vendor_doc_a" in inputs:
        return check_document_versions([inputs["vendor_doc_a"], inputs["vendor_doc_b"]])
    if "offered_document_version" in inputs:
        return check_document_is_current(inputs["offered_document_version"],
                                         inputs["current_approved_version"])
    if "vendor_claim" in inputs:
        return check_vendor_claim(inputs["vendor_claim"])
    if "vendor_output" in inputs:
        return check_vendor_output(inputs["vendor_output"])
    if "vendor_status" in inputs:
        return check_vendor_available(inputs["vendor_status"])
    if "proposed_consumer_wording" in inputs:
        return check_proposed_wording(inputs["proposed_consumer_wording"],
                                      inputs.get("scorer_emitted_codes"),
                                      taxonomy, wording)
    if "reason_codes" in inputs:
        return resolve(inputs["reason_codes"], inputs.get("outcome", "deny"),
                       taxonomy, wording)
    raise KeyError(f"unhandled input shape for {case['eval_id']}: {sorted(inputs)}")


def _expects_refusal(expected):
    """Their vocabulary for "this must not go out", across every spelling used."""
    return any(bool(expected.get(k)) for k in (
        "decision_refused", "refused", "stop", "claim_rejected", "escalated",
        "decision_refused_or_rewritten_to_mapped_emitted_code"))


def run_acceptance(package_dir=None) -> dict:
    taxonomy, wording = load_taxonomy(package_dir), load_wording(package_dir)
    results = []

    for case in load_acceptance_evaluations(package_dir):
        eval_id, category = case["eval_id"], case["category"]
        expected = case["expected_outcome"]

        if category in _DELEGATED:
            results.append({"eval_id": eval_id, "category": category,
                            "status": "delegated", "owner": _DELEGATED[category]})
            continue

        try:
            got = _run_case(case, taxonomy, wording)
        except ReasonRefused as exc:
            if not _expects_refusal(expected):
                results.append({"eval_id": eval_id, "category": category,
                                "status": "FAIL",
                                "detail": f"unexpected refusal: {exc}"})
                continue
            missing = set(case.get("required_refusals") or ()) - set(exc.refusals)
            needs_escalation = bool(case.get("required_escalations"))
            if missing:
                detail = f"refusal categories missing: {sorted(missing)}"
            elif needs_escalation and not exc.escalate:
                detail = "the case requires human escalation and the refusal did not"
            else:
                detail = ""
            results.append({"eval_id": eval_id, "category": category,
                            "status": "pass" if not detail else "FAIL",
                            "detail": detail})
            continue

        if _expects_refusal(expected):
            results.append({"eval_id": eval_id, "category": category,
                            "status": "FAIL",
                            "detail": f"expected a refusal, got {got!r}"})
            continue

        # Compare only the keys the client actually supplied. Asserting on a key
        # they left out would be this repository inventing an expectation and
        # then grading itself against it.
        detail = ""
        if isinstance(got, dict):
            for key, want in expected.items():
                if key in got and got[key] != want:
                    detail = f"{key}: expected {want!r}, got {got[key]!r}"
                    break
        results.append({"eval_id": eval_id, "category": category,
                        "status": "pass" if not detail else "FAIL", "detail": detail})

    return {
        "banner": TRAINING_BANNER,
        "package_version": PACKAGE_VERSION,
        "total": len(results),
        "passed": sum(1 for r in results if r["status"] == "pass"),
        "delegated": sum(1 for r in results if r["status"] == "delegated"),
        "failed": [r for r in results if r["status"] == "FAIL"],
        "results": results,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    require_intact()
    report = run_acceptance()

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"=== GOVERNANCE ACCEPTANCE — {report['banner']} ===")
        print(f"package {report['package_version']}")
        for r in report["results"]:
            tail = r.get("owner") or r.get("detail") or ""
            print(f"  {r['eval_id']:<9} {r['status']:<10} {r['category']:<28} {tail}")
        print(f"{report['passed']} resolved here, {report['delegated']} delegated, "
              f"{len(report['failed'])} failed")
        print(f"=== END — {report['banner']} ===")
    return 1 if report["failed"] else 0


if __name__ == "__main__":  # pragma: no cover - exercised via its test
    raise SystemExit(main())
