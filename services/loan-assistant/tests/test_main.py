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


# --- the 422 body is what a loan officer reads --------------------------------

def test_summary_refusal_returns_the_readable_detail_not_the_developer_string(monkeypatch):
    """The route must return `exc.detail`, not `str(exc)`.

    Written at the ROUTE deliberately, and it took two attempts to make it
    real. The first version asserted on the exception object, so reverting the
    route to `str(exc)` left every test passing while the UI went back to
    showing `app_id=7577 is missing ['income', 'employment_years']`. The second
    used the wrong path (`/summary/{id}`), got a 404, and skipped its own
    assertions behind an `if status == 422`. Both were caught by checking, not
    by reading.
    """
    import httpx
    from app.llm_client import LLMInsufficientDataError

    def _refuse(app_data):
        raise LLMInsufficientDataError(
            "app_id=7577 is missing ['income', 'employment_years'] — refusing",
            detail=("No summary: this application has no recorded income or "
                    "employment history. A risk summary without it would be "
                    "guesswork, so none was generated."),
        )

    class _Resp:
        status_code = 200
        def json(self): return {"id": 7577, "amount": 500, "term_months": 12}
        def raise_for_status(self): pass

    monkeypatch.setattr(main, "summarize_application", _refuse)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _Resp())

    resp = client.post("/applications/7577/summary",
                       headers={"X-User-Role": "underwriter"})

    # Unconditional: a wrong status is a failure, not a reason to skip.
    assert resp.status_code == 422, f"expected 422, got {resp.status_code}: {resp.text}"
    body = resp.json()["detail"]
    assert "app_id=" not in body, f"internal identifier reached the UI: {body!r}"
    assert "[" not in body, f"Python list literal reached the UI: {body!r}"
    assert "guesswork" in body
