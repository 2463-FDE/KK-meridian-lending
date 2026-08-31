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
def test_the_two_routes_now_go_through_an_approval(route):
    """§1's central claim, inverted for the third and final time.

    It first required that these routes have NO approval -- true, and written to
    fail the day that changed, with the failure as the instruction to rewrite §1.
    It has now fired for the last time: the control is implemented, so the test
    pins the control instead of its absence.

    If this fails, someone has removed the approval step and the money routes
    move money alone again.
    """
    src = SERVICING_MAIN.read_text(encoding="utf-8")
    assert route in src, f"{route} no longer exists"
    assert "maker_checker.propose" in src, (
        "the money routes no longer raise proposals -- D8 has reopened"
    )
    resolve = src[src.index("def resolve_movement("):]
    assert "maker_checker.resolve" in resolve, (
        "there is no resolve endpoint, so a proposal can never be approved"
    )


def test_servicing_now_verifies_the_principal_it_used_to_ignore():
    """This assertion has been inverted, and the inversion is the point.

    It used to require that `adjust_balance` accept `x_user_role` and never check
    a role -- true while the csr/admin rule lived only at the gateway, and the
    exact shape of D8's identity half. The guard was written to fail when
    maker-checker landed, with the failure as the instruction to rewrite §1.

    Half of that arrived: servicing now verifies a gateway-signed principal and
    applies csr/admin itself. Left alone, this test would have gone on *requiring*
    the old system -- pinning "servicing checks nothing" as truth and failing any
    attempt to correct the spec. A guard that outlives its subject stops
    protecting the document and starts protecting the mistake.

    So it now pins the CURRENT boundary, and will fail the same way again when the
    approval step lands -- at which point the residue in §1 must shrink to nothing.
    """
    src = SERVICING_MAIN.read_text(encoding="utf-8")
    adjust = src[src.index("def adjust_balance("):]
    adjust = adjust[:adjust.index("@app.post", 1)] if "@app.post" in adjust[1:] else adjust[:1500]

    # `require_staff_principal` since the cutover, not `require_money_principal`:
    # proposing moves nothing, so every staff role may do it, while approving is
    # a separate authority decided against the configured threshold. Either guard
    # satisfies the property this test exists for -- that a VERIFIED human is
    # required -- and neither may be absent.
    assert ("require_staff_principal" in adjust
            or "require_money_principal" in adjust), (
        "adjust_balance no longer verifies a human principal -- if the role check "
        "moved back to the gateway alone, the direct-to-servicing bypass is "
        "reopened"
    )
    assert "x_principal_assertion" in adjust, (
        "the handler does not take the signed assertion, so whatever it verifies "
        "is not coming from the gateway"
    )
    # The header is still accepted, and still not trusted: it is passed to the
    # guard only so a disagreement with the signature can be refused.
    assert "x_user_role" in adjust, "the role header is no longer even inspected"
    assert "claimed_role=x_user_role" in adjust, (
        "x_user_role is read without being cross-checked against the signature -- "
        "that is the bypass spec 0002 REQ-ID-8 forbids"
    )


#: Where origination-service defines the roles that can act. It used to be a
#: literal inside `routers/applications.py`; RF-25's API needed a NARROWER set
#: for manual DTI, so the definitions moved to one module rather than being
#: copied. This guard follows them there, and reads EVERY role set it declares --
#: the point is that no role can act without appearing in spec 0002's matrix, so
#: a second set added beside the first must be covered too.
ORIGINATION_ROLES = (REPO / "services" / "origination-service" / "app"
                     / "staff_auth.py")


def _declared_roles() -> set:
    src = ORIGINATION_ROLES.read_text(encoding="utf-8")
    matches = re.findall(r"^[A-Z_]*ROLES\s*=\s*frozenset\(\{([^}]*)\}\)",
                         src, re.MULTILINE)
    assert matches, (
        f"no role set found in {ORIGINATION_ROLES.name}. If the definitions moved "
        "again, this guard must follow them -- a matrix checked against a file "
        "that no longer declares roles passes by finding nothing")
    roles = set()
    for body in matches:
        roles |= {r.strip().strip("\"'") for r in body.split(",") if r.strip()}
    return roles


def test_the_roles_the_spec_matrixes_are_the_roles_that_exist():
    """The matrix must use real roles. An invented hierarchy is how a control
    spec becomes unimplementable."""
    spec = SPEC.read_text(encoding="utf-8")
    roles = _declared_roles()
    assert roles, "no roles parsed"
    assert "csr" in roles and "underwriter" in roles and "admin" in roles, roles
    for role in roles:
        assert role in spec, (
            f"the role {role!r} exists in the system but not in spec 0002's role "
            f"matrix, so the matrix does not cover everyone who can act"
        )


def test_d8_is_recorded_as_closed_now_that_the_control_exists():
    """This guard has done its job and is inverted for the last time.

    It required D8 to read **Open**, and existed so that closing it would fail
    here -- the failure being the instruction to rewrite spec 0002 section 1
    rather than to delete the test. That is exactly what happened: the control
    landed, this fired, and section 1 became history.

    What it pins now is the pair. The register and the code must agree about
    whether a second approver exists, in both directions -- an entry marked Fixed
    with no approval path is the overclaim, and an entry left Open beside a
    working control is the understatement.
    """
    row = next(l for l in DEBT.read_text(encoding="utf-8").splitlines()
               if l.startswith("| **D8**"))
    servicing = SERVICING_MAIN.read_text(encoding="utf-8")
    implemented = "maker_checker.resolve" in servicing

    assert implemented, "the approval path is gone; D8 must go back to Open"
    assert "**Fixed.**" in row, (
        "D8 still reads as open while adjust-balance and waive-fee raise "
        "proposals that a second person must approve"
    )
    assert "not Lending Operations policy" in row.replace("**", ""), (
        "D8 claims the control without recording that its limits are cohort/demo "
        "configuration -- a reader would take 500.00 for approved policy"
    )


def test_the_spec_states_no_UNAPPROVED_threshold_amount():
    """A figure may appear only as an approved decision, never as a bare default.

    The original rule was absolute: no dollar figure anywhere near either limit.
    That was right while nobody had chosen one -- an earlier draft set
    MAKER_CHECKER_ADMIN_THRESHOLD to $500.00 that no policy document supported,
    which is what `policies/underwriting_guidelines.md` had already been
    corrected for.

    A figure has since been approved by the project owner for the cohort/demo
    environment. So the durable requirement is not "no number" but
    "no number without a named approval and a stated scope" -- and the scope
    matters as much as the approval: these are explicitly NOT production Lending
    Operations policy, and a reader who takes them for policy has been misled by
    this document.
    """
    text = SPEC.read_text(encoding="utf-8")
    flat = " ".join(text.split())

    threshold_lines = [
        line for line in text.splitlines()
        if "MAKER_CHECKER_ADMIN_THRESHOLD" in line or "MAKER_CHECKER_MAX_DELTA" in line
    ]
    assert threshold_lines, "the spec no longer names a configured limit at all"

    # No code default, ever. A default is how an unset variable silently becomes
    # a policy decision nobody made.
    for line in threshold_lines:
        if "default" in line.lower():
            assert "no default" in line.lower(), (
                f"the spec gives a configured money limit a default: {line.strip()!r}"
            )

    # Any figure present must sit inside the approval block, and that block must
    # say who approved it and that it is not production policy.
    if any(re.search(r"\d+\.\d{2}", line) for line in threshold_lines):
        assert "approved" in flat.lower(), (
            "the spec states a monetary limit with no record of anyone approving it"
        )
        assert "NOT production" in flat or "not production" in flat.lower(), (
            "the spec states an approved figure without marking it as "
            "cohort/demo rather than Lending Operations policy -- a reader would "
            "take it for policy, which is the overclaim this repository has "
            "already had to correct in policies/underwriting_guidelines.md"
        )


def test_the_approved_limits_are_not_hardcoded_in_the_schema():
    """Approved configuration must stay configuration.

    A CHECK carrying 500.00 would make a policy change a migration, and would
    freeze a cohort/demo figure into the shape of the data.
    """
    migration = (REPO / "db" / "migrations" / "0036_pending_movements.sql")
    if not migration.is_file():
        pytest.skip("the maker-checker schema has not landed yet")
    sql = migration.read_text(encoding="utf-8")
    body = sql[sql.index("CREATE TABLE IF NOT EXISTS pending_movements"):]
    for figure in ("500.00", "5000.00"):
        assert figure not in body, (
            f"db/migrations/0036 encodes the configured limit {figure}; the "
            f"limits are read from the environment at runtime"
        )


def test_the_limits_fail_closed_when_missing():
    text = SPEC.read_text(encoding="utf-8")
    assert "REQ-CFG-2" in text and "refuse to start" in text, (
        "the spec does not require the service to fail closed on a missing "
        "control limit, so an unset variable silently becomes 'no threshold'"
    )


def test_the_spec_and_the_implementation_agree_on_what_exists():
    """The last inversion of this file's central guard.

    It required the spec to state that nothing in it was implemented, and to be
    marked a Draft. Both were right for as long as they were true, and the test
    was built to fail the day they stopped -- which is now. What it pins instead
    is that the document and the code cannot disagree about whether the control
    exists.
    """
    text = SPEC.read_text(encoding="utf-8")
    servicing = SERVICING_MAIN.read_text(encoding="utf-8")
    implemented = "maker_checker.resolve" in servicing

    assert implemented, (
        "no resolve path in servicing -- if the control was removed, this "
        "document must go back to describing it as a proposal"
    )
    assert "**Implemented.**" in text or "Status:** **Implemented" in text, (
        "the spec still reads as an unbuilt draft while the control is live. A "
        "reader deciding whether the second-approver rule exists would get the "
        "wrong answer from the document of record."
    )
    assert "does not implement it" not in text, (
        "the spec still says it does not implement the control"
    )


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


# --- ADR 0011 is on `main`, so agreement is checked directly ------------------

ADR_0011 = REPO / "adr" / "0011-maker-checker-for-servicing-adjustments.md"

#: Retained rather than deleted: the guard costs nothing and states the
#: precondition. It no longer skips -- the ADR is on `main` alongside this test,
#: so every case below runs. The old skip reason named the branch the ADR was
#: being written on, which stopped being true the moment it merged.
adr_present = pytest.mark.skipif(
    not ADR_0011.is_file(),
    reason="adr/0011-maker-checker-for-servicing-adjustments.md is not present",
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
