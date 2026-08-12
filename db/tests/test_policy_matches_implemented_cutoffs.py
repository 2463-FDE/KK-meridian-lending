"""The published underwriting policy must not name a cutoff the code never applies.

`adr/0007` found `underwriting_guidelines.md` publishing DTI bands and a
fraud-flag rule that nothing in the system implements. That is the worst kind of
documentation defect on a lending platform: a denied applicant, or a regulator,
reads it as a description of what was checked, and `decision_events` rows carry
reason codes that implicitly agree.

The owner resolved it by amending the policy (ADR 0007, Resolution). This keeps it
resolved. It compares the numbers the policy publishes against the numbers
`decision-service` actually branches on, so re-adding a cutoff to the policy
without implementing it fails here.

Deliberately narrow. This is not a general policy-vs-code checker -- it is a check
on the specific claim that drifted, in the direction that matters: **the policy
must not promise more than the code does.**
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
POLICY = REPO / "policies" / "underwriting_guidelines.md"
DECISION = REPO / "services" / "decision-service" / "app" / "decision.py"

#: Terms that name an input the system does not have. Present in the policy's
#: *cutoff* section they are a false claim; present in a section that says they
#: are not applied they are documentation, which is why the check is scoped to
#: the cutoff list rather than the whole file.
UNIMPLEMENTED_INPUTS = ("dti", "debt-to-income", "debt to income", "fraud flag")


def _policy() -> str:
    return POLICY.read_text(encoding="utf-8")


def _cutoff_section() -> str:
    """The numbered 'Apply policy cutoffs' list -- what the system claims to do."""
    text = _policy()
    start = text.index("Apply policy cutoffs")
    end = text.index("Counteroffer is permitted", start)
    return text[start:end]


def test_the_cutoff_list_names_no_input_the_system_does_not_have(_=None):
    section = _cutoff_section().lower()
    named = [term for term in UNIMPLEMENTED_INPUTS if term in section]
    assert not named, (
        f"the policy's decision cutoffs name {named}, which nothing in "
        f"decision-service computes. A denied applicant reads this as what was "
        f"checked. Implement it, or document it under 'not currently applied' "
        f"(see adr/0007)."
    )


@pytest.mark.parametrize("threshold", ["660", "600"])
def test_every_score_cutoff_in_the_policy_exists_in_the_code(threshold):
    """The other direction: the numbers the policy publishes must be real."""
    assert threshold in _cutoff_section(), f"the policy no longer publishes {threshold}"
    assert re.search(rf"\b{threshold}\b", DECISION.read_text(encoding="utf-8")), (
        f"the policy publishes a {threshold} cutoff that decision.py does not branch on"
    )


def test_dti_is_still_defined_somewhere():
    """Amending the policy must not silently delete the definition.

    The gap is real and open -- Meridian does not assess debt-to-income and
    arguably should. Deleting the definition would turn an acknowledged gap into
    an invisible one, which is a worse outcome than the overclaim it replaced.
    """
    text = _policy().lower()
    assert "debt-to-income" in text, "the DTI definition was removed rather than scoped"
    assert "not currently applied" in text, (
        "the policy no longer says DTI is unapplied, so a reader cannot tell "
        "whether it is enforced"
    )


def test_the_adr_records_the_resolution():
    adr = (REPO / "adr" / "0007-underwriting-policy-dti-fraud-gap.md").read_text(encoding="utf-8")
    assert "## Resolution" in adr, (
        "adr/0007 does not record which way the gap was closed, so a future reader "
        "cannot tell an amended policy from an unfixed one"
    )
