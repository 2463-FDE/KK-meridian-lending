"""Tests for the policy Q&A chat -- the answer-generation feature built on top
of Week 2's retrieve()/classify_answerable() gate (rag_eval.py), the feature
that gate's own eval harness deliberately deferred building (adr/0005).

Core guarantee under test: a question classify_answerable() marks ungrounded
must never reach the LLM at all -- no hallucination risk, matching the same
gate rag_eval.py's eval set already proves against a fixed query list.
"""
import json

import pytest

from app import llm_client, policy_chat
from app.llm_client import LLMCostGuardError
from app.policy_chat import PolicyChatResponseError, answer_policy_question


def test_not_answerable_question_never_calls_llm(monkeypatch):
    # Same case rag_eval.EVAL_QUERIES already documents as expect_answer=False.
    def _boom(client, prompt, system=None):
        raise AssertionError("LLM must not be called for an ungrounded question")

    monkeypatch.setattr(llm_client, "call_api", _boom)

    result = answer_policy_question("why was application 6012 denied")

    assert result.answerable is False
    assert result.source_chunk_id is None
    assert "record" in result.answer.lower()


def test_answerable_question_returns_grounded_answer(monkeypatch):
    canned = json.dumps({"answerable": True, "answer": "The late fee is $35."})
    monkeypatch.setattr(llm_client, "call_api", lambda client, prompt, system=None: canned)
    monkeypatch.setattr(llm_client, "make_client", lambda: object())

    result = answer_policy_question("what is the late fee amount")

    assert result.answerable is True
    assert result.answer == "The late fee is $35."
    assert result.source_chunk_id is not None
    # Real retrieved excerpt, not just its id -- lets a reader verify the
    # answer against actual policy text instead of trusting it on faith.
    assert result.source_text
    assert "35" in result.source_text


def test_not_answerable_question_has_no_source_text(monkeypatch):
    def _boom(client, prompt, system=None):
        raise AssertionError("LLM must not be called for an ungrounded question")

    monkeypatch.setattr(llm_client, "call_api", _boom)

    result = answer_policy_question("why was application 6012 denied")

    assert result.source_text is None


def test_answer_handles_markdown_fenced_response(monkeypatch):
    fenced = "```json\n" + json.dumps({"answerable": True, "answer": "18 years old."}) + "\n```"
    monkeypatch.setattr(llm_client, "call_api", lambda client, prompt, system=None: fenced)
    monkeypatch.setattr(llm_client, "make_client", lambda: object())

    result = answer_policy_question("what is the minimum age to apply for a loan")

    assert result.answerable is True
    assert result.answer == "18 years old."


def test_question_is_redacted_before_use(monkeypatch):
    # Isolate the redaction property from retrieval-gate behavior: force the
    # answerable path regardless of how the SSN-bearing text tokenizes, since
    # that's classify_answerable()'s concern, not this test's.
    monkeypatch.setattr(policy_chat, "classify_answerable", lambda query, hits: True)

    captured = {}

    def _capture(client, prompt, system=None):
        captured["prompt"] = prompt
        return json.dumps({"answerable": True, "answer": "18 years old."})

    monkeypatch.setattr(llm_client, "call_api", _capture)
    monkeypatch.setattr(llm_client, "make_client", lambda: object())

    ssn_question = "what is the minimum age to apply for a loan, my ssn is 412-55-9981"
    answer_policy_question(ssn_question)

    assert "412-55-9981" not in captured["prompt"]


# --- Review finding: bool(data.get("answerable", True)) trusted sloppy model
# output two ways -- Python's bare bool("false") is True (any non-empty string
# is truthy), and a missing key silently defaulted to answerable=True. Now
# parsed through a strict Pydantic model instead.

def test_string_false_is_parsed_as_real_false(monkeypatch):
    # The bug this guards against: Python's builtin bool("false") is True.
    assert bool("false") is True  # sanity-check the bug still exists in Python itself
    canned = json.dumps({"answerable": "false", "answer": "Not covered by this excerpt."})
    monkeypatch.setattr(llm_client, "call_api", lambda client, prompt, system=None: canned)
    monkeypatch.setattr(llm_client, "make_client", lambda: object())
    monkeypatch.setattr(policy_chat, "classify_answerable", lambda query, hits: True)

    result = answer_policy_question("what is the late fee amount")

    assert result.answerable is False
    assert result.source_chunk_id is None
    assert result.source_text is None


def test_missing_answerable_field_fails_closed(monkeypatch):
    # No "answerable" key at all -- must be rejected, not defaulted to True.
    canned = json.dumps({"answer": "Some answer with no answerable flag."})
    monkeypatch.setattr(llm_client, "call_api", lambda client, prompt, system=None: canned)
    monkeypatch.setattr(llm_client, "make_client", lambda: object())
    monkeypatch.setattr(policy_chat, "classify_answerable", lambda query, hits: True)

    with pytest.raises(PolicyChatResponseError):
        answer_policy_question("what is the late fee amount")


def test_empty_answer_string_is_rejected(monkeypatch):
    canned = json.dumps({"answerable": True, "answer": ""})
    monkeypatch.setattr(llm_client, "call_api", lambda client, prompt, system=None: canned)
    monkeypatch.setattr(llm_client, "make_client", lambda: object())
    monkeypatch.setattr(policy_chat, "classify_answerable", lambda query, hits: True)

    with pytest.raises(PolicyChatResponseError):
        answer_policy_question("what is the late fee amount")


# Review finding: policy_chat.py sent an arbitrary-length question straight to
# the LLM, skipping the MAX_INPUT_TOKENS guard summarize_application() enforces --
# a large-but-schema-valid question could trigger an oversized paid request.
def test_oversized_question_fails_cost_guard_before_calling_llm(monkeypatch):
    def _boom(client, prompt, system=None):
        raise AssertionError("LLM must not be called once the cost guard trips")

    monkeypatch.setattr(llm_client, "call_api", _boom)
    monkeypatch.setattr(policy_chat, "classify_answerable", lambda query, hits: True)
    monkeypatch.setattr(llm_client, "MAX_INPUT_TOKENS", 50)

    with pytest.raises(LLMCostGuardError):
        answer_policy_question("what is the minimum age to apply for a loan " * 20)


def test_non_dict_json_response_is_rejected(monkeypatch):
    # A valid JSON value that isn't an object at all (e.g. the model returned
    # a bare string or array) must fail closed, not crash with an unhandled
    # TypeError from ** on a non-mapping.
    canned = json.dumps(["not", "an", "object"])
    monkeypatch.setattr(llm_client, "call_api", lambda client, prompt, system=None: canned)
    monkeypatch.setattr(llm_client, "make_client", lambda: object())
    monkeypatch.setattr(policy_chat, "classify_answerable", lambda query, hits: True)

    with pytest.raises(PolicyChatResponseError):
        answer_policy_question("what is the late fee amount")
