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

**No model runs, and the gate that decides is still exercised.** The first
version of this file asserted only that `search_underwriting_policy()` returned
hits, and that was too weak in a way that mattered: `answer_policy_question()`
does not answer because retrieval found something, it answers because
`classify_answerable()` accepts what retrieval found. Those are different
predicates, and a chip can pass the first and fail the second.

It did. "What loan terms are available?" retrieved `underwriting_guidelines.md`
happily and the classifier rejected it -- term coverage 1/3 against a 0.6 gate --
so the shipped chip landed on the refusal path while this guard reported it
fine. Review found it; the guard did not, because the guard was checking the
easier thing.

So the assertion now runs the SAME pair the route runs, `retrieve()` then
`classify_answerable()`, against the same corpus state. Both are deterministic
and neither needs credentials. Whether Claude then phrases a good answer is a
separate question that does need them, and that remains untested here -- said
plainly rather than implied.
"""
import pathlib
import re

import pytest

from app import policy_chat
from app.policy_tool import ALLOWED_DOCUMENTS, search_underwriting_policy
from app.rag_eval import classify_answerable, retrieve

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


def _route_would_answer(question: str) -> bool:
    """The decision `answer_policy_question()` makes, made the same way.

    Deliberately not a re-implementation: it calls the same `retrieve()` and
    `classify_answerable()` against the same `_corpus_state()` the route builds,
    so a change to either moves this guard with it.
    """
    chunks, embedder, idf = policy_chat._corpus_state()
    return classify_answerable(question, retrieve(question, chunks, embedder, idf))


def test_the_gate_rejects_something_so_the_check_can_fail():
    """Guard the guard.

    If `classify_answerable()` accepted everything, every assertion below would
    pass while proving nothing. This pins a question the corpus genuinely cannot
    answer, so the gate is known to be discriminating when the chips are put
    through it.
    """
    assert not _route_would_answer(
        "What is the current share price of an unrelated public company?"
    ), "the answerability gate accepted an out-of-corpus question"


@pytest.mark.parametrize("question", _shipped_examples())
def test_each_example_chip_is_answerable_by_the_route(question):
    """The property that actually matters: the route would ANSWER this chip.

    Retrieval finding something is necessary and not sufficient -- the shipped
    "What loan terms are available?" retrieved fine and was refused, which is
    exactly the failure this now catches.
    """
    assert _route_would_answer(question), (
        f"the shipped example chip {question!r} is refused by "
        "`classify_answerable()`, so clicking it demonstrates the refusal path "
        "rather than the feature. Retrieval finding something is not enough: "
        "rephrase the chip toward the words the corpus uses, or change the "
        "corpus."
    )


@pytest.mark.parametrize("question", _shipped_examples())
def test_each_example_chip_retrieves_policy(question):
    """The narrower half, kept because it localises a failure.

    When a chip breaks, this says whether retrieval found nothing at all or
    found something the gate then rejected -- two different repairs.
    """
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
