"""Route-level tests for main.py's exception -> HTTP status mapping.

No TestClient-based tests existed for this service before -- every other test
file exercises the underlying functions directly. Added specifically to cover
the review-pass finding that /policy-chat didn't map LLMTimeoutError/
LLMResponseError the way /applications/{id}/summary does right above it in
the same file.
"""
from fastapi.testclient import TestClient

from app import main
from app.llm_client import LLMCostGuardError, LLMResponseError, LLMTimeoutError

client = TestClient(main.app, raise_server_exceptions=False)


# Review finding: an arbitrary-length question skipped the MAX_INPUT_TOKENS guard
# summarize_application() enforces, risking oversized paid LLM calls. PolicyChatIn
# now caps question length at the schema layer (422)...
def test_policy_chat_rejects_question_over_schema_max_length():
    resp = client.post("/policy-chat", json={"question": "x" * 4001})

    assert resp.status_code == 422


# ...and answer_policy_question() itself enforces the real token-budget guard
# (against system prompt + retrieved excerpt), mapped to 400 same as /summary.
def test_policy_chat_maps_llm_cost_guard_error_to_400(monkeypatch):
    def _boom(question):
        raise LLMCostGuardError("Estimated input tokens (5000) exceeds guard (2000).")

    monkeypatch.setattr(main, "answer_policy_question", _boom)

    resp = client.post("/policy-chat", json={"question": "what is the late fee amount"})

    assert resp.status_code == 400


def test_policy_chat_maps_llm_timeout_to_504(monkeypatch):
    def _boom(question):
        raise LLMTimeoutError("LLM call timed out after 20s")

    monkeypatch.setattr(main, "answer_policy_question", _boom)

    resp = client.post("/policy-chat", json={"question": "what is the late fee amount"})

    assert resp.status_code == 504


def test_policy_chat_maps_llm_response_error_to_502(monkeypatch):
    def _boom(question):
        raise LLMResponseError("Could not parse LLM response")

    monkeypatch.setattr(main, "answer_policy_question", _boom)

    resp = client.post("/policy-chat", json={"question": "what is the late fee amount"})

    assert resp.status_code == 502


def test_unexpected_exception_returns_clean_500(monkeypatch):
    # Same catch-all pattern as decision-service/gateway -- an exception type
    # no route explicitly handles must still return a controlled response, not
    # whatever FastAPI's default unhandled-exception behavior would otherwise do.
    def _boom(question):
        raise RuntimeError("something genuinely unexpected")

    monkeypatch.setattr(main, "answer_policy_question", _boom)

    resp = client.post("/policy-chat", json={"question": "what is the late fee amount"})

    assert resp.status_code == 500
    assert resp.json() == {"detail": "internal error"}


def test_policy_chat_returns_200_on_success(monkeypatch):
    from app.schemas import PolicyAnswer

    monkeypatch.setattr(
        main,
        "answer_policy_question",
        lambda question: PolicyAnswer(answerable=True, answer="The late fee is $35."),
    )

    resp = client.post("/policy-chat", json={"question": "what is the late fee amount"})

    assert resp.status_code == 200
    assert resp.json()["answer"] == "The late fee is $35."
