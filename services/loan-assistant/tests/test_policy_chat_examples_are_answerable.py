"""The example chips must retrieve policy, not demonstrate the refusal path.

Policy Chat ships three example questions. They are the first thing anyone
clicks, so a chip that comes back "I could not find anything in the policy
documents" teaches, on the very first interaction, that the assistant does not
know things -- when the whole point of the refusal path is that it declines what
it cannot GROUND. A chip that cannot be answered is a demo of the failure case
wearing the clothes of a suggestion.

**The questions are read out of the component rather than copied here.** A copy
would let the shipped chips and the tested chips drift apart, and the drift
would be invisible: this file would keep passing against questions nobody sees.
Same reasoning as the other derived guards in this repository -- the expectation
comes from the source, so adding or changing a chip needs no edit here and a
chip that stops retrieving fails.

**No model runs.** `search_underwriting_policy` is the retrieval half and is
deterministic; whether Claude then phrases an answer is a separate question and
needs credentials. What is asserted is the part that must be true for an answer
to be possible at all: each chip finds allowlisted policy text. That is
deliberately weaker than "the chip produces a good answer" and it is the
strongest claim available without a live model -- said plainly rather than
implied.
"""
import pathlib
import re

import pytest

from app.policy_tool import ALLOWED_DOCUMENTS, search_underwriting_policy

COMPONENT = (
    pathlib.Path(__file__).resolve().parents[3]
    / "frontend" / "components" / "PolicyChat.tsx"
)


def _shipped_examples() -> list[str]:
    """The chips as the component actually renders them."""
    src = COMPONENT.read_text(encoding="utf-8")
    block = re.search(r"const EXAMPLES = \[(.*?)\];", src, re.S)
    assert block, (
        "PolicyChat.tsx no longer declares `const EXAMPLES = [...]`. If the "
        "chips moved, point this test at their new home -- a guard that cannot "
        "find its subject passes for the wrong reason."
    )
    return re.findall(r'"([^"]+)"', block.group(1))


def test_the_component_still_ships_example_chips():
    """Guard the guard: an empty list would make every case below vacuous."""
    examples = _shipped_examples()

    assert len(examples) >= 3, (
        f"expected at least three example chips, found {examples}"
    )


@pytest.mark.parametrize("question", _shipped_examples())
def test_each_example_chip_retrieves_policy(question):
    result = search_underwriting_policy(question)

    assert result["status"] == "hit", (
        f"the shipped example chip {question!r} retrieves nothing from the "
        "policy corpus, so clicking it demonstrates the refusal path rather "
        "than the feature. Either the chip or the corpus needs to change."
    )
    assert result["hit_count"] >= 1
    assert result["excerpts"], "a hit with no excerpts is not evidence"


@pytest.mark.parametrize("question", _shipped_examples())
def test_each_example_chip_is_answered_from_an_allowlisted_document(question):
    """The chips must not depend on a document the tool may not read.

    `ALLOWED_DOCUMENTS` is an allowlist rather than "everything under
    policies/", so a chip whose answer lives outside it would retrieve nothing
    in production however well it reads.
    """
    result = search_underwriting_policy(question)

    documents = {e["document"] for e in result["excerpts"]}
    assert documents, f"no document answered {question!r}"
    # `document` already carries the .md suffix ("fee_schedule.md"), which the
    # allowlist is keyed on. Appending one produced "fee_schedule.md.md" and
    # failed every chip -- the assertion was wrong, not the retrieval.
    outside = {d for d in documents if d not in ALLOWED_DOCUMENTS}
    assert not outside, (
        f"{question!r} was answered from {sorted(outside)}, which the policy "
        "tool's allowlist does not admit"
    )
