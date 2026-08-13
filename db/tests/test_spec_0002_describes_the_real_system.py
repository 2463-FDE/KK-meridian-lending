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


@pytest.mark.parametrize("requirement", [f"REQ-ID-{n}" for n in range(1, 11)])
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


def test_the_spec_requires_a_non_forgeable_server_validated_principal():
    text = SPEC.read_text(encoding="utf-8")
    for phrase in (
        "server-side Redis session",
        "asymmetric private key",
        "independently verify",
        "audience",
        "shared service token",
        "SHALL NOT authenticate a human",
    ):
        assert phrase in text, f"identity boundary omits {phrase!r}"


def test_the_spec_is_honest_that_the_principal_mechanism_is_not_implemented():
    text = SPEC.read_text(encoding="utf-8")
    assert "no signed principal assertion" in text
    assert "does not claim the present headers satisfy it" in " ".join(text.split())


def test_forged_headers_with_a_valid_service_token_are_covered():
    text = SPEC.read_text(encoding="utf-8").lower()
    for case in (
        "backend cannot forge a human",
        "forged role cannot override",
        "forged identity cannot hide self-approval",
        "missing human principal fails closed",
        "invalid human principal fails closed",
        "machine service remains a machine service",
    ):
        assert case in text, f"missing acceptance scenario: {case}"


# --- proposal-side validity: the review's central finding ---------------------


@pytest.mark.parametrize("requirement", [f"REQ-VAL-{n}" for n in range(1, 16)])
def test_the_proposal_validity_requirements_are_all_present(requirement):
    """Guarding only the approval step puts the whole control on one tired human.

    The role matrix lets a CSR raise a proposal, and approval copies the
    proposal's fields straight into the ledger -- so an unconstrained proposal
    that gets rubber-stamped under queue pressure becomes a real, irreversible
    money movement. These requirements are what stop a bad request entering the
    queue at all.
    """
    assert requirement in SPEC.read_text(encoding="utf-8"), (
        f"{requirement} is gone from spec 0002, so proposal creation is "
        "unconstrained in whatever it governed"
    )


@pytest.mark.parametrize("criterion", [f"AC-{n}" for n in range(1, 30)])
def test_every_acceptance_criterion_is_present(criterion):
    """A numbered criterion that vanishes takes its rejection path with it, and
    the gap is invisible: the remaining numbers still read as a complete list."""
    text = SPEC.read_text(encoding="utf-8")
    assert f"**{criterion}**" in text, f"{criterion} is missing from spec 0002"


def test_resolution_revalidates_a_queued_proposals_complete_target():
    text = SPEC.read_text(encoding="utf-8").lower()
    for case in (
        "queued proposal cannot execute after its loan closes",
        "queued proposal cannot execute after servicing is removed",
        "unrecognised status introduced while queued fails closed",
    ):
        assert case in text, f"missing queued-state acceptance scenario: {case}"
    req = text[text.index("**req-val-15**"):text.index("**req-val-15**") + 900]
    for invariant in ("loan still exists", "balances", "status", "target-authorization"):
        assert invariant in req, f"resolution does not revalidate {invariant}"


def test_the_spec_invents_no_threshold_amount():
    """The reported finding, and the one most likely to come back.

    An earlier draft set MAKER_CHECKER_ADMIN_THRESHOLD to $500.00. Nobody chose
    that number -- it is in no policy document and no stakeholder stated it. A
    specification that invents a monetary control limit and writes it in the tone
    of a requirement is what `policies/underwriting_guidelines.md` already had to
    be corrected for, where published DTI cutoffs described nothing the code
    evaluated.
    """
    text = SPEC.read_text(encoding="utf-8")
    threshold_lines = [
        line for line in text.splitlines()
        if "MAKER_CHECKER_ADMIN_THRESHOLD" in line or "MAKER_CHECKER_MAX_DELTA" in line
    ]
    assert threshold_lines, "the spec no longer names a configured threshold at all"
    for line in threshold_lines:
        if "default" not in line.lower():
            continue
        assert "no default" in line.lower(), (
            f"the spec gives a configured money limit a default: {line.strip()!r}"
        )
    # No bare dollar figure may be attached to either limit.
    for line in threshold_lines:
        assert not re.search(r"\$\s?\d", line), (
            f"the spec states a dollar figure for a limit it does not own: "
            f"{line.strip()!r}"
        )
        assert not re.search(r"\b\d+\.\d+\b", line), (
            f"the spec states a bare numeric figure for a limit it does not own: "
            f"{line.strip()!r}"
        )


def test_the_limits_fail_closed_when_missing():
    text = SPEC.read_text(encoding="utf-8")
    assert "REQ-CFG-2" in text and "refuse to start" in text, (
        "the spec does not require the service to fail closed on a missing "
        "control limit, so an unset variable silently becomes 'no threshold'"
    )


def test_the_spec_does_not_claim_the_control_is_implemented():
    """This document specifies a control. Merging it changes nothing about what
    the running system permits, and saying so is the point: this codebase has
    twice shipped a document that read as a description of a working control and
    was a description of an intention.
    """
    text = SPEC.read_text(encoding="utf-8")
    assert "does not implement it" in text.lower(), (
        "the spec does not state that nothing in it is implemented yet"
    )
    assert "Draft" in text, "the spec is no longer marked as a draft"


def test_the_spec_agrees_with_adr_0011_on_what_is_frozen():
    """The two documents describe one control. ADR 0011's transition trigger
    freezes `reason` and `requested_at`; a spec whose immutability criterion
    omitted them would have an implementer building a weaker control and passing
    their own acceptance tests.
    """
    text = SPEC.read_text(encoding="utf-8")
    ac12 = next(line for line in text.splitlines() if line.startswith("- **AC-12**"))
    block = text[text.index(ac12):text.index(ac12) + 900]
    for column in ("reason", "requested_at", "requested_role", "resolved_role"):
        assert column in block, (
            f"AC-12 does not require {column!r} to be immutable, while ADR 0011's "
            "transition trigger freezes it"
        )


def test_the_role_the_ledger_records_is_specified():
    """ADR 0011 overwrites both actor_id AND actor_role from the proposal.
    A spec that only tracked the ids would leave the role caller-supplied on the
    entry whose purpose is recording who authorised the movement."""
    text = SPEC.read_text(encoding="utf-8")
    assert "resolved_role" in text and "actor_role" in text, (
        "the spec does not say where the ledger entry's actor_role comes from"
    )


# --- and once ADR 0011 is on the same branch, agreement is checked directly ---

ADR_0011 = REPO / "adr" / "0011-maker-checker-for-servicing-adjustments.md"

adr_present = pytest.mark.skipif(
    not ADR_0011.is_file(),
    reason="ADR 0011 is on PR #20's branch, not this one",
)


@adr_present
@pytest.mark.parametrize("column", ["reason", "requested_at", "requested_role",
                                    "resolved_role"])
def test_the_spec_and_the_adr_freeze_the_same_fields(column):
    """Checked against the ADR itself, not against the spec's own restatement.

    Two documents describing one control drift silently, and this pair is the
    likeliest to: the ADR states what the database enforces and the spec states
    what the API must do about it. Skipped while they live on different branches;
    binding the moment both are on one.
    """
    adr = ADR_0011.read_text(encoding="utf-8")
    start = adr.index("CREATE FUNCTION pending_movements_single_transition")
    frozen = adr[start:adr.index("$$ LANGUAGE plpgsql;", start)]
    assert f"NEW.{column}" in frozen, (
        f"ADR 0011 no longer freezes {column!r}, so spec 0002's AC-12 requires "
        "an immutability the database does not provide"
    )
    assert column in SPEC.read_text(encoding="utf-8")


@adr_present
def test_the_spec_does_not_contradict_the_adr_on_self_approval():
    adr = ADR_0011.read_text(encoding="utf-8")
    assert "no_self_approval" in adr
    spec = SPEC.read_text(encoding="utf-8")
    assert "including admin" in spec, (
        "the spec no longer says the no-self-approval rule has no exception, "
        "while the ADR enforces it with a table constraint that has none"
    )
