"""No LLM-generated categorical risk label may reach staff.

`ee473f4` removed the numeric thresholds from the summary prompt and left the
concept: `_LLMOutput` still required `risk_tier` to be one of
low/medium/high/decline, so the model had to invent a classification boundary
whatever the prompt said -- and staff saw the result as a coloured chip that
looked policy-backed.

The rule this enforces: a categorical risk label may be shown to staff only if a
published deterministic policy rule produces it. `policies/underwriting_guidelines.md`
publishes the model-score bands and nothing that maps an application to a tier, so
today the answer is that no such label may come from the model at all.

What staff should read is already there and deterministic -- the decision outcome
and model score from decision-service. Reg B adverse-action reasons come from
those drivers, never from a summary.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
ASSISTANT = REPO / "services" / "loan-assistant" / "app"
FRONTEND = REPO / "frontend"
POLICY = REPO / "policies" / "underwriting_guidelines.md"

#: Field names that would carry a model-assigned classification.
LABEL_FIELDS = ("risk_tier", "risk_grade", "risk_band", "risk_category",
                "risk_rating", "risk_level")


def test_the_llm_response_contract_declares_no_risk_label():
    """The contract is what forces the model to classify. Removing the numbers
    from the prompt while leaving the field required changes nothing."""
    for name in ("llm_client.py", "schemas.py"):
        src = (ASSISTANT / name).read_text(encoding="utf-8")
        for field in LABEL_FIELDS:
            declared = re.search(rf"^\s+{field}\s*:\s*(?!#)", src, re.M)
            assert not declared, (
                f"{name} still declares {field} as a response field, so the model "
                f"must produce a category no published rule defines"
            )


def test_the_prompt_does_not_ask_for_a_rating():
    src = (ASSISTANT / "llm_client.py").read_text(encoding="utf-8")
    system = src[src.index("_SYSTEM = "):src.index("class _LLMOutput")]
    asked = [l for l in system.splitlines()
             if re.search(r"\brisk[_ ](tier|grade|rating|band|level)\b", l, re.I)
             and not re.search(r"do not|never|no published", l, re.I)]
    assert not asked, f"the prompt still asks for a risk label: {asked}"


def test_no_staff_screen_renders_a_model_risk_label():
    """A chip is a stronger claim than a sentence: it reads as a verdict."""
    offenders = []
    for path in list(FRONTEND.rglob("*.tsx")) + list(FRONTEND.rglob("*.ts")):
        if "node_modules" in path.parts or ".next" in path.parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for field in LABEL_FIELDS:
            for line in text.splitlines():
                if f"summary.{field}" in line or f"RISK_TONE" in line:
                    offenders.append(f"{path.name}: {line.strip()[:70]}")
    assert not offenders, (
        "a staff screen renders a model-generated risk label: " + "; ".join(offenders)
    )


def test_a_label_would_be_permitted_if_policy_published_the_rule():
    """Guards against over-correction, and states the condition.

    This is not a ban on risk tiers. It is a ban on a tier with no rule behind
    it. If Lending Ops publishes and approves a mapping, this test's premise
    changes and the field can come back -- deterministically, from
    decision-service rather than from prose.
    """
    policy = POLICY.read_text(encoding="utf-8").lower()
    publishes_tier_rule = any(
        w in policy for w in ("risk tier", "risk grade", "risk band")
    )
    assert not publishes_tier_rule, (
        "the policy now publishes a risk-tier rule -- this test's premise has "
        "changed, and a deterministic implementation of that rule is what should "
        "produce the label"
    )


def test_adverse_action_reasons_do_not_come_from_the_summary():
    """Reg B requires the specific principal reason for a denial. A model's prose
    is not a reason code, and wiring one into the notice would be a compliance
    defect rather than a UX shortcut."""
    origination = REPO / "services" / "origination-service" / "app"
    for path in origination.rglob("*.py"):
        src = path.read_text(encoding="utf-8", errors="replace")
        for line in src.splitlines():
            if "adverse_action" in line and re.search(r"summary|llm|assistant", line, re.I):
                pytest.fail(
                    f"{path.name} appears to source an adverse-action reason from "
                    f"a summary: {line.strip()[:80]}"
                )
