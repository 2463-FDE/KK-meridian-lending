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


#: The retired numeric bands, as they were actually written. A cutoff removed
#: from the policy and left in a docstring is still published -- FastAPI serves
#: docstrings on /docs, so it describes the criterion to anyone reading the API.
RETIRED_CUTOFFS = ("43-50%", "43–50%", "dti <= 43%", "dti ≤ 43%",
                   "dti > 50%", "dti 43")

#: Marks a mention as a record of the retirement rather than a claim. A file is
#: allowed -- encouraged -- to say the rule USED to exist; it may not state it as
#: current.
RETIREMENT_MARKERS = ("retired", "used to publish", "used to say", "adr/0007",
                      "no longer", "was removed")


def _api_facing_files():
    """Docstrings and schema comments a caller can read.

    Routers because FastAPI publishes their docstrings on /docs; schemas because
    they are the request/response contract. Not every file in the repo -- the
    claim being guarded is one made to an API consumer.
    """
    services = REPO / "services"
    files = list(services.glob("*/app/routers/*.py"))
    files += list(services.glob("*/app/schemas.py"))
    return sorted(f for f in files if f.name != "__init__.py")


def _offending_lines(path):
    lines = path.read_text(encoding="utf-8").splitlines()
    out = []
    for i, line in enumerate(lines):
        low = line.lower()
        if not any(c in low for c in RETIRED_CUTOFFS):
            continue
        # Context window: a retirement note may put the marker on an adjacent
        # line, which is how the correction was actually written.
        window = " ".join(lines[max(0, i - 3):i + 4]).lower()
        if any(m in window for m in RETIREMENT_MARKERS):
            continue
        out.append(f"{path.name}:{i + 1}: {line.strip()}")
    return out


@pytest.mark.parametrize("path", _api_facing_files(), ids=lambda p: f"{p.parent.parent.parent.name}/{p.name}")
def test_no_api_facing_doc_publishes_a_retired_cutoff(path):
    """The gap the first version of this test left open.

    It scanned the policy document only. The same retired band was still in
    `review_application`'s FastAPI docstring and in `ReviewIn`'s schema comment --
    so the policy said one thing and /docs said another, and a staff member
    resolving a referral could read that it was raised on a criterion nothing ever
    evaluated.
    """
    offending = _offending_lines(path)
    assert not offending, (
        "API-facing documentation publishes a retired DTI cutoff: "
        + "; ".join(offending)
        + ". The policy retired it (adr/0007 Resolution) because nothing computes "
          "a DTI. Remove it, or mark it explicitly as retired."
    )


def test_the_api_scan_found_files_to_scan():
    """Guards the guard: an empty file list passes the parametrized test by
    having nothing to run."""
    files = _api_facing_files()
    assert len(files) >= 5, f"only {len(files)} API-facing file(s) found: {files}"
    assert any("applications.py" in f.name for f in files), (
        "the origination router is not being scanned, and it is where the retired "
        "cutoff actually was"
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
