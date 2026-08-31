"""Policy Chat must not make the runtime look compliant with a policy it does not apply.

THE DEFECT, OBSERVED RATHER THAN SUPPOSED. Asked "What is the late fee?" against a
live provider on 2026-08-31, Policy Chat answered with the client's decided rule --
"the lesser of $35.00 or 5% of the unpaid scheduled principal plus interest for
that installment, charged at most once per missed scheduled installment after the
grace period" -- marked "Grounded in policy" and cited to `fee_schedule.md#2.0`.

That is the rule the client decided on 2026-08-29. It is NOT what the code
charges: `delinquency.py` computes `min($35, 5% of balances.past_due)`, a wider
base with no per-installment cap, and the cutover is blocked on two authorities
nobody in this repository holds (`docs/DEBT.md` D23). A staff member reading that
answer would conclude a borrower is charged the decided rule.

THE MECHANISM, TRACED RATHER THAN GUESSED. It was not retrieval failing:

  * `fee_schedule.md#2.0` -- the policy row -- ends "*The code does not yet
    implement this -- see 'Current implementation differs' below.*"
  * that section is a SEPARATE chunk, `fee_schedule.md#3.0` onwards;
  * `answer_policy_question` built its prompt from `hits[0]` alone.

So the model was handed a pointer to text it did not have, and summarised the
half-caveat in the row away. Retrieval had already surfaced the section as hit 1;
context assembly discarded it.

WHAT IS ASSERTED HERE. Context assembly and the system contract are deterministic,
so they are asserted directly. What a model does with a correct prompt is not, so
the model is stubbed and the cases check that the caveat reaches it and that its
answer is passed through -- not that a real provider phrases it a particular way.
`scripts/check_ai_live.sh` is where a live answer is exercised.
"""
import json

import pytest

from app import corpus as pt
from app import policy_chat as pc


@pytest.fixture(scope="module")
def corpus():
    return pc._corpus_state()


def _context(question, corpus):
    chunks, embedder, idf = corpus
    from app.rag_eval import retrieve
    hits = retrieve(question, chunks, embedder, idf)
    return hits, pc._context_for(hits[0], hits, chunks)


# --------------------------------------------------------------------------
# The caveat reaches the model.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("question", [
    "What is the late fee?",
    "How much is the late fee?",
    "When do you charge a late fee?",
])
def test_the_late_fee_context_carries_the_implementation_status(question, corpus):
    """The exact question that produced the defect, and two phrasings of it."""
    hits, context = _context(question, corpus)
    assert "Current implementation differs" in context, (
        "the status section is not in the prompt, so the model is again being "
        "handed a pointer to text it does not have")
    # And not merely the heading: the part that says what the code DOES.
    assert "past_due" in context, (
        "the context says the policy is unimplemented but not what is implemented "
        "instead, so an answer cannot say what a borrower is actually charged")
    assert len(context) > len(hits[0]["text"]), "nothing was added at all"


def test_the_policy_itself_is_still_in_the_context(corpus):
    """The caveat must not displace the answer. Both halves, or neither is useful."""
    _, context = _context("What is the late fee?", corpus)
    assert "5% of that installment's unpaid scheduled principal" in context
    assert "$35.00" in context


def test_the_status_section_is_marked_in_the_corpus_not_matched_by_prose(corpus):
    """The marker is a HEADING, an authoring act, not a phrase scan.

    A prose match would fire on any paragraph that happened to discuss
    implementation -- including this repository's own commentary about D23.
    """
    chunks, _, _ = corpus
    flagged = [c["chunk_id"] for c in chunks if c.get("implementation_status")]
    assert flagged, "no chunk is marked as implementation status"
    assert all(c.startswith("fee_schedule.md#") for c in flagged), flagged
    # A chunk may REFERENCE the section without being it -- the policy row ends
    # "see 'Current implementation differs' below", which is a pointer, not a
    # heading. What must be marked is a chunk that OPENS the section, i.e. carries
    # the heading at the start of a line.
    import re as _re
    heading = _re.compile(r"^#{1,6}\s*Current implementation differs", _re.MULTILINE)
    for chunk in chunks:
        if chunk.get("implementation_status"):
            continue
        assert not heading.search(chunk["text"]), (
            f"{chunk['chunk_id']} opens the status section but is not marked")


# --------------------------------------------------------------------------
# The negative control: unrelated answers are untouched.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("question", [
    "What are the standard loan terms?",
    "What score requires manual review?",
])
def test_an_unrelated_question_gets_no_implementation_text(question, corpus):
    """No global disclaimer. A question whose evidence carries no such claim is
    answered from exactly the excerpt it was answered from before."""
    hits, context = _context(question, corpus)
    assert context == hits[0]["text"], (
        "implementation-status text was added to a question whose own evidence "
        "makes no claim about implementation")
    assert "Current implementation differs" not in context


def test_the_system_prompt_requires_the_caveat_to_survive(corpus):
    """The prompt rule is half the mechanism and is asserted as contract.

    Context assembly gets the evidence in front of the model; this is what stops
    the model compressing it back out, which is exactly what it did when the row's
    own half-caveat was present and the section was not.
    """
    # Whitespace-normalised: the rule wraps across lines in the source, and a
    # test that depended on where it wrapped would fail on a reflow.
    system = " ".join(pc._SYSTEM.lower().split())
    assert "does not" in system and "implement" in system
    assert "must say so" in system
    assert "never present the policy as describing what the system does today" in system


# --------------------------------------------------------------------------
# End to end through answer_policy_question, with the provider stubbed.
# --------------------------------------------------------------------------

def _stub_provider(monkeypatch, capture):
    """Capture the prompt the provider would receive; return a fixed JSON reply."""
    monkeypatch.setattr(pc.llm_client, "make_client", lambda: object())

    def _call(client, prompt, system=None):
        capture["prompt"] = prompt
        capture["system"] = system
        return json.dumps({
            "answerable": True,
            "answer": ("Policy: at most one fee per missed installment, the lesser "
                       "of $35.00 and 5% of that installment's unpaid scheduled "
                       "principal and interest. Meridian's system does not "
                       "currently apply this rule."),
        })

    monkeypatch.setattr(pc.llm_client, "call_api", _call)


def test_the_answer_path_sends_the_caveat_and_keeps_the_citation(monkeypatch):
    capture = {}
    _stub_provider(monkeypatch, capture)

    answer = pc.answer_policy_question("What is the late fee?")

    assert answer.answerable is True
    # The citation is unchanged: still the policy chunk, not the status section.
    assert answer.source_chunk_id == "fee_schedule.md#2.0", answer.source_chunk_id
    # The prompt carried both halves.
    #
    # Asserted on `past_due`, NOT on the phrase "Current implementation differs":
    # the policy row itself ends "see 'Current implementation differs' below", so
    # that phrase is present even when the section is NOT appended. A mutation
    # reverting context assembly to `hits[0]` alone passed against that weaker
    # assertion -- which is how this was found. `past_due` appears only in the
    # section that says what the code actually charges.
    assert "past_due" in capture["prompt"], (
        "the status section did not reach the model; the prompt contains the "
        "policy's pointer to it but not the section itself")
    assert "$35.00" in capture["prompt"]
    # And the model's answer is passed through, not rewritten here.
    assert "does not currently apply" in answer.answer


def test_an_unrelated_answer_path_sends_no_implementation_text(monkeypatch):
    capture = {}
    _stub_provider(monkeypatch, capture)

    pc.answer_policy_question("What score requires manual review?")

    assert "Current implementation differs" not in capture["prompt"], (
        "an unrelated question's prompt now carries late-fee implementation text")


def test_the_prompt_stays_inside_the_cost_guard(monkeypatch):
    """Appending a section must not push the request past the guard.

    The guard runs after context assembly, so a section large enough to trip it
    would turn a working question into an error. Asserted rather than assumed
    because `_STATUS_CONTEXT_CHARS` is a number somebody can raise.
    """
    capture = {}
    _stub_provider(monkeypatch, capture)
    pc.answer_policy_question("What is the late fee?")
    estimated = pc.llm_client._estimate_tokens(capture["system"] + capture["prompt"])
    assert estimated < pc.llm_client.MAX_INPUT_TOKENS, estimated


# --------------------------------------------------------------------------
# Document scoping, on a synthetic corpus.
#
# Found by mutation rather than by reading: deleting `and c.get("doc_id") ==
# doc_id` from `_context_for` left every test above GREEN, because exactly one
# document in the real corpus carries a status section, so cross-document
# leakage has nothing to leak yet. It would the day a second policy file gains
# one -- and the failure would be an answer about fees carrying an unrelated
# document's implementation caveat, which is a new false statement rather than a
# missing true one.
#
# `_context_for` is a pure function over dicts, so this is asserted on chunks
# built here rather than on the corpus on disk.
# --------------------------------------------------------------------------

_POINTER_TEXT = ("Late payment fee: the lesser of $35.00 and 5%. *The code does "
                 "not yet implement this.*")


def _chunk(chunk_id, doc_id, text, status=False):
    return {"chunk_id": chunk_id, "doc_id": doc_id, "text": text,
            "implementation_status": status}


def test_another_documents_status_section_is_not_borrowed():
    top = _chunk("a.md#1.0", "a.md", _POINTER_TEXT)
    chunks = [
        top,
        _chunk("b.md#9.0", "b.md",
               "## Current implementation differs\nb.md's runtime charges nothing.",
               status=True),
    ]
    context = pc._context_for(top, [top], chunks)
    assert context == top["text"], (
        "a.md's answer picked up b.md's implementation-status section. The two "
        "documents describe different runtimes, so that is a false statement "
        "about a.md rather than a missing true one")


def test_the_documents_own_status_section_is_still_appended():
    """The other half of the same scoping -- it must not simply append nothing."""
    top = _chunk("a.md#1.0", "a.md", _POINTER_TEXT)
    own = _chunk("a.md#2.0", "a.md",
                 "## Current implementation differs\na.md's runtime charges a flat fee.",
                 status=True)
    other = _chunk("b.md#9.0", "b.md",
                   "## Current implementation differs\nb.md's runtime charges nothing.",
                   status=True)
    context = pc._context_for(top, [top], [top, own, other])
    assert "a.md's runtime charges a flat fee" in context
    assert "b.md's runtime charges nothing" not in context


def test_a_pointer_with_no_status_section_anywhere_changes_nothing():
    """A policy file may claim it is unimplemented without publishing a section.

    Nothing is synthesised to fill the gap: the answer is grounded in the excerpt
    it was always grounded in, and the system prompt still requires the model to
    repeat the claim the excerpt itself makes.
    """
    top = _chunk("a.md#1.0", "a.md", _POINTER_TEXT)
    assert pc._context_for(top, [top], [top]) == top["text"]


# --------------------------------------------------------------------------
# Codex review M1: the EVIDENCE panel must show what the answer was grounded in.
#
# The prompt carried the appended status section while `PolicyAnswer.source_text`
# still returned `hits[0]["text"]`. So the answer could say the code charges
# against `past_due` while "Show evidence" displayed only the policy row, which
# does not contain that phrase -- a reader could not check the runtime half of
# the answer, and the runtime half is the entire point of the change.
# --------------------------------------------------------------------------

def test_the_evidence_shown_is_the_evidence_the_model_was_given(monkeypatch):
    capture = {}
    _stub_provider(monkeypatch, capture)

    answer = pc.answer_policy_question("What is the late fee?")

    assert answer.source_text is not None
    assert "past_due" in answer.source_text, (
        "the evidence panel shows only the policy row while the answer speaks "
        "about the runtime; a reader cannot verify the half of the answer this "
        "change exists to add")
    assert answer.source_text in capture["prompt"], (
        "the evidence shown is not the text the model was given")
    # The citation still names the policy chunk -- the caveat is context, not a
    # second source that would change what the answer is attributed to.
    assert answer.source_chunk_id == "fee_schedule.md#2.0"


def test_an_unrelated_answer_shows_only_its_own_excerpt(monkeypatch):
    capture = {}
    _stub_provider(monkeypatch, capture)
    answer = pc.answer_policy_question("What score requires manual review?")
    assert "Current implementation differs" not in (answer.source_text or "")


# --------------------------------------------------------------------------
# Codex review M2: the agent's bounded tool is the second surface, and it was
# serving the pointer without the section on 14 of 30 query/top_k combinations.
# --------------------------------------------------------------------------

_FEE_QUERIES = ["late fee", "late payment fee", "fee for missing a payment",
                "what happens if I pay late", "grace period", "missed installment",
                "fee schedule"]


@pytest.mark.parametrize("query", _FEE_QUERIES)
@pytest.mark.parametrize("k", [1, 2, 3])
def test_the_agent_tool_never_serves_the_pointer_without_the_section(query, k):
    """Every combination that used to fail, and the ones that did not.

    `top_k=1` is the case that made a substitution strategy unworkable: replacing
    the lowest-ranked hit would have replaced the policy row itself, answering a
    fee question with only the caveat.
    """
    from app.policy_tool import search_underwriting_policy

    result = search_underwriting_policy(query, top_k=k)
    texts = [e["excerpt"] for e in result["excerpts"]]
    if not any(pt.points_to_implementation_status(t) for t in texts):
        return
    assert any("Current implementation differs" in t for t in texts), (
        f"{query!r} at top_k={k} hands the agent a policy row that says the code "
        "does not implement it, with no section saying what the code does")
    assert any("past_due" in t for t in texts), (
        "the caveat arrived without the paragraph naming the actual charge; "
        "half a caveat lets an answer say a rule is unimplemented without being "
        "able to say what a borrower is charged instead")


@pytest.mark.parametrize("k", [1, 2, 3, 10, 500])
def test_the_tools_own_bounds_still_hold(k):
    """The fix had to fit INSIDE the bounds, not renegotiate them silently.

    `hit_count <= MAX_TOP_K` is unchanged, which is why the section is appended
    to an excerpt rather than added as an extra hit. The per-excerpt ceiling did
    move -- deliberately, as the sum of two separate budgets -- and an excerpt
    carrying no caveat is still bounded by the original number alone.
    """
    from app import policy_tool

    result = policy_tool.search_underwriting_policy("late fee", top_k=k)
    assert result["hit_count"] <= policy_tool.MAX_TOP_K
    for excerpt in result["excerpts"]:
        text = excerpt["excerpt"]
        assert len(text) <= policy_tool.MAX_EXCERPT_CHARS + policy_tool.MAX_STATUS_CHARS
        if "Current implementation differs" not in text:
            assert len(text) <= policy_tool.MAX_EXCERPT_CHARS


def test_both_surfaces_recognise_the_caveat_with_one_definition():
    """One expression, in `corpus`, used by the chat path and the agent path.

    Two copies would be two definitions of "does this policy admit it is not
    implemented", and the day they disagreed one surface would go back to
    stating an unimplemented rule as current practice.
    """
    from app import policy_tool

    assert pt.points_to_implementation_status(
        "The code does not yet implement this.")
    assert not pt.points_to_implementation_status("The APR is 12%.")
    assert policy_tool.corpus.points_to_implementation_status is \
        pt.points_to_implementation_status
    assert "_CAVEAT_POINTER" not in pc.__dict__, (
        "policy_chat kept a private copy of the pointer expression")


def test_the_appended_section_stops_rather_than_skipping_ahead(monkeypatch):
    """Document order under a budget that actually bites.

    Found by mutation: turning the `break` into a `continue` left every test
    above green, because the real section fits inside `MAX_STATUS_CHARS` and
    nothing is ever truncated. It matters the moment something is -- a `continue`
    lets a small later paragraph jump ahead of a large earlier one, and this
    section is written to be read top to bottom: it says the policy and the code
    differ, and only THEN what the code charges. Skipping the second to fit the
    fifth produces a caveat that reads as if it were complete.
    """
    from app import policy_tool

    chunks = [
        {"chunk_id": "d.md#1.0", "doc_id": "d.md", "text": "policy row",
         "implementation_status": False},
        {"chunk_id": "d.md#2.0", "doc_id": "d.md",
         "text": "## Current implementation differs\n" + "A" * 200,
         "implementation_status": True},
        {"chunk_id": "d.md#3.0", "doc_id": "d.md", "text": "B" * 400,
         "implementation_status": True},
        {"chunk_id": "d.md#4.0", "doc_id": "d.md", "text": "C" * 20,
         "implementation_status": True},
    ]
    monkeypatch.setattr(policy_tool, "MAX_STATUS_CHARS", 300)
    out = policy_tool._with_status(
        "The code does not yet implement this.", "d.md", chunks)

    assert "A" * 200 in out, "the opening of the section was dropped"
    assert "B" * 400 not in out, "a paragraph larger than the budget was included"
    assert "C" * 20 not in out, (
        "a later, smaller paragraph jumped ahead of the one that did not fit; "
        "the section is ordered prose, not a bag of paragraphs")
