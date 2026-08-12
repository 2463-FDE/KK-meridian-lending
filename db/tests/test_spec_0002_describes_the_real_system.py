"""Spec 0002's description of the CURRENT system must stay true.

A spec written before the implementation has one failure mode that matters: its
"current state" section quietly becomes history while still reading as fact, and
then the acceptance criteria are written against a system that no longer exists.
This repository has had that defect three times in other documents -- a policy
publishing DTI cutoffs nothing evaluated, a README describing dropped columns, a
runbook describing committed secrets.

So the falsifiable claims in §1 are asserted here. When maker-checker is
implemented these tests fail, and that failure is the signal to rewrite §1 as
"what it was" rather than to delete the test.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SPEC = REPO / "specs" / "0002-maker-checker-self-approval.md"
SERVICING_MAIN = REPO / "services" / "servicing-service" / "app" / "main.py"
ORIGINATION_ROUTER = (REPO / "services" / "origination-service" / "app"
                      / "routers" / "applications.py")
DEBT = REPO / "docs" / "DEBT.md"


def test_the_spec_exists_and_names_what_it_closes():
    text = SPEC.read_text(encoding="utf-8")
    assert "D8" in text, "the spec does not say which debt item it closes"
    assert "Out of scope" in text, (
        "the spec has no out-of-scope section -- an approval spec without one "
        "reads as though delegation and notification are covered"
    )


@pytest.mark.parametrize("route", ["adjust-balance", "waive-fee"])
def test_the_two_routes_still_have_no_approval(route):
    """§1's central claim. If this fails, maker-checker exists and §1 is history."""
    src = SERVICING_MAIN.read_text(encoding="utf-8")
    assert route in src, f"{route} no longer exists -- spec 0002 §1 is out of date"
    assert "pending_movements" not in src, (
        "servicing-service references pending_movements, so an approval path "
        "exists and spec 0002's 'current state' is no longer current"
    )


def test_the_role_header_is_still_accepted_and_unused():
    """The specific shape of D8: the endpoints take a role and never read it,
    which is worse than not taking one -- it looks like an authorisation check."""
    src = SERVICING_MAIN.read_text(encoding="utf-8")
    adjust = src[src.index("def adjust_balance("):]
    adjust = adjust[:adjust.index("@app.post", 1)] if "@app.post" in adjust[1:] else adjust[:600]
    assert "x_user_role" in adjust, "adjust_balance no longer accepts a role header"
    assert "_STAFF_ROLES" not in adjust and "require_staff" not in adjust.lower(), (
        "adjust_balance now checks the role -- spec 0002 §1 must be updated"
    )


def test_the_roles_the_spec_matrixes_are_the_roles_that_exist():
    """The matrix must use real roles. An invented hierarchy is how a control
    spec becomes unimplementable."""
    spec = SPEC.read_text(encoding="utf-8")
    src = ORIGINATION_ROUTER.read_text(encoding="utf-8")
    m = re.search(r"_STAFF_ROLES\s*=\s*\{([^}]*)\}", src)
    assert m, "could not find _STAFF_ROLES in origination-service"
    roles = {r.strip().strip("\"'") for r in m.group(1).split(",") if r.strip()}
    assert roles, "no roles parsed"
    for role in roles:
        assert role in spec, (
            f"the role {role!r} exists in the system but not in spec 0002's role "
            f"matrix, so the matrix does not cover everyone who can act"
        )


def test_d8_is_still_open():
    """If D8 closes, this spec has been implemented and §1 needs rewriting."""
    row = [l for l in DEBT.read_text(encoding="utf-8").splitlines()
           if l.startswith("| **D8**")]
    assert row, "D8 is missing from the debt register"
    assert "**Open**" in row[0], (
        "D8 is no longer open -- spec 0002 describes a system that has changed"
    )


def test_the_spec_states_its_own_limit():
    """The direct-INSERT bypass has to be in the spec, not discovered later.

    Every other approval-shaped claim in this repository that omitted its limit
    turned out to be an overclaim.
    """
    text = SPEC.read_text(encoding="utf-8")
    assert "direct `INSERT`" in text or "direct INSERT" in text, (
        "the spec does not state that a direct database insert bypasses "
        "maker-checker, which is the boundary of the whole control"
    )
    assert "REVOKE" in text, (
        "the spec does not explain why privilege-based enforcement is unavailable"
    )


# --- the identity trust boundary --------------------------------------------


@pytest.mark.parametrize("requirement", ["REQ-ID-1", "REQ-ID-2", "REQ-ID-3",
                                         "REQ-ID-4", "REQ-ID-5", "REQ-ID-6"])
def test_the_identity_requirements_are_all_present(requirement):
    """The whole control reduces to "are these two the same person?".

    If identity can be asserted by the caller, "a different approver" means "a
    different string in a header" and maker-checker is a naming convention.
    """
    assert requirement in SPEC.read_text(encoding="utf-8"), (
        f"{requirement} is missing -- the spec does not pin down where "
        f"requested_by and resolved_by come from"
    )


def test_the_spec_forbids_trusting_client_supplied_identity():
    text = SPEC.read_text(encoding="utf-8")
    assert "never from a value supplied by the client" in text
    assert "X-User-" in text, "the spec does not name the headers in question"


def test_the_gateway_really_does_strip_inbound_identity_headers():
    """REQ-ID-2 claims this already holds. Hold it to the code.

    If the gateway stopped stripping, a browser client could supply its own
    X-User-Id and the spec's central assumption would be false while still
    reading as satisfied.
    """
    src = (REPO / "services" / "gateway" / "app" / "main.py").read_text(encoding="utf-8")
    assert 'startswith("x-user-")' in src, (
        "the gateway no longer strips inbound X-User-* headers, so spec 0002 "
        "REQ-ID-2 describes something that is not true"
    )
    assert 'headers["X-User-Id"]' in src, (
        "the gateway no longer stamps X-User-Id from the resolved session"
    )


def test_the_spec_covers_the_adversarial_cases():
    """Spoofing, unresolved identity, self-approval, authority, concurrency and
    immutable evidence -- the cases a happy-path spec omits."""
    text = SPEC.read_text(encoding="utf-8").lower()
    for case, why in [
        ("spoofed requester", "a forged requester header"),
        ("spoofed role", "a forged role header"),
        ("cannot be resolved", "identity that does not resolve"),
        ("self-approval", "the requester approving their own proposal"),
        ("insufficient authority", "an approver below the threshold"),
        ("race", "two approvers at once"),
        ("cannot be amended", "audit evidence being edited after the fact"),
    ]:
        assert case in text, f"no acceptance case for {why}"


def test_the_service_credential_is_not_treated_as_a_user_credential():
    """The bypass this spec has to forbid: reading the role header because the
    request carried the internal token. That token is shared by every backend --
    it authenticates a service, not a human."""
    text = SPEC.read_text(encoding="utf-8")
    assert "service* credential" in text or "service credential" in text or            "not a *user* credential" in text or "not a user credential" in text, (
        "the spec does not distinguish the shared service token from a user "
        "credential, which is the escalation path an implementer would take"
    )
