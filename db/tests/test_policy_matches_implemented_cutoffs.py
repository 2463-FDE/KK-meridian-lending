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
    """Every surface that PUBLISHES the rule to a human or an API consumer.

    The first version of this scan covered routers and schemas, and the retired
    cutoff was still live in three places it did not look: the underwriting UI's
    reason placeholder -- labelled "shown to the applicant if denied", so it
    suggested a DTI justification for an adverse-action notice -- two DDL
    comments, and the loan-assistant system prompt, which regenerates the claim
    on every officer summary.

    A prompt is the worst of them. It is not documentation describing behaviour;
    it MANUFACTURES the claim at runtime, so a stale rule there is published
    fresh to staff on every call.
    """
    services = REPO / "services"
    files = list(services.glob("*/app/routers/*.py"))
    files += list(services.glob("*/app/schemas.py"))
    # LLM prompts: a claim factory, not a description of one.
    files += list(services.glob("*/app/llm_client.py"))
    # UI text: placeholders, labels and options are published rules.
    frontend = REPO / "frontend" / "app"
    if frontend.exists():
        files += [f for f in frontend.rglob("*.tsx") if "node_modules" not in f.parts]
    # Schema comments ship with the database and are read by whoever debugs it.
    files += list((REPO / "db" / "init").glob("*.sql"))
    files += list((REPO / "db" / "migrations").glob("*.sql"))
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


def test_the_scan_covers_every_kind_of_published_surface():
    """Guards the guard, and names the surfaces rather than counting files.

    A count passes when a whole CATEGORY is missing. Each of these is a surface
    where the retired cutoff was actually found.
    """
    files = _api_facing_files()
    names = [str(f) for f in files]
    for needle, why in [
        ("applications.py", "the origination router -- where the cutoff was in a docstring"),
        ("schemas.py", "the request contract"),
        ("llm_client.py", "the summary prompt, which regenerates the claim per call"),
        (".tsx", "UI text -- the reason placeholder shown to staff and quoted to applicants"),
        (".sql", "schema comments, read by whoever debugs the database"),
    ]:
        assert any(needle in n for n in names), (
            f"the scan does not cover {needle} ({why})"
        )
    assert len(files) >= 10, f"only {len(files)} file(s) scanned"


def test_no_adverse_action_reason_suggests_an_unevaluated_criterion():
    """The no-ship case, stated as its own test.

    The underwriting reason box is labelled "shown to the applicant if denied".
    A placeholder there is a suggested adverse-action reason, so offering a DTI
    justification put a criterion the system never evaluates into a Reg B notice.
    Staff can still type anything -- what is fixed is the system PROPOSING it.
    """
    ui = REPO / "frontend" / "app" / "underwriting" / "[appId]" / "page.tsx"
    if not ui.exists():                                   # pragma: no cover
        pytest.skip("underwriting page not present")
    text = ui.read_text(encoding="utf-8")
    for m in re.finditer(r'placeholder="([^"]*)"', text):
        assert "dti" not in m.group(1).lower(), (
            f"the reason placeholder suggests a DTI-based adverse-action reason: "
            f"{m.group(1)!r}"
        )


def test_the_summary_prompt_does_not_ask_for_a_ratio_it_is_not_given():
    """A prompt is a claim factory. Asking the model for a debt-to-income ratio
    when no debt figure is in the payload produces a fabricated number on every
    call -- in the same prompt that tells it not to invent information."""
    prompt_file = REPO / "services" / "loan-assistant" / "app" / "llm_client.py"
    src = prompt_file.read_text(encoding="utf-8")
    system = src[src.index("_SYSTEM = "):src.index("class _LLMOutput")]
    rule_lines = [l for l in system.splitlines()
                  if "dti" in l.lower() or "debt-to-income" in l.lower()]
    offending = [l for l in rule_lines
                 if not any(m in l.lower() for m in ("do not", "not given", "fabricated"))]
    assert not offending, (
        f"the system prompt still instructs the model to reason about DTI: "
        f"{offending}"
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


def test_the_prompt_states_no_numeric_threshold_that_policy_does_not_publish():
    """A threshold in the prompt is a staff-facing rule with no rule behind it.

    The first fix for this PR swapped an unapproved DTI cutoff for an unapproved
    LOAN-TO-INCOME one -- 0.25 low, 0.5 high -- which is the same defect moved from
    the policy document into the runtime prompt. Staff on the underwriting screen
    see a risk chip that looks policy-backed and is prompt-only, and in manual
    review that can sway an approve or deny with nothing auditable behind it.

    So: any numeric threshold the prompt applies must also appear in the published
    policy. Today the policy publishes the model-score bands and nothing else, so
    the prompt may state no cutoffs at all.
    """
    src = (REPO / "services" / "loan-assistant" / "app" / "llm_client.py").read_text(encoding="utf-8")
    system = src[src.index("_SYSTEM = "):src.index("class _LLMOutput")]
    policy = _policy()

    # Any ratio- or percentage-shaped cutoff in a rule line.
    numbers = set(re.findall(r"[<>]\s*([0-9]+(?:\.[0-9]+)?)\s*%?", system))
    unpublished = [n for n in numbers if n not in policy]
    assert not unpublished, (
        f"the summary prompt applies thresholds {sorted(unpublished)} that the "
        f"published policy does not contain. Either publish and approve them, or "
        f"make the field descriptive."
    )
