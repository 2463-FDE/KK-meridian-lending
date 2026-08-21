"""Loan Assistant service — FastAPI.

Wraps the Week 1 redactor + guardrailed LLM client behind an HTTP API so the gateway
and frontend can reach it. Fetches application data from origination-service (the
system of record) rather than talking to Postgres directly.
"""
import logging
import os

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, Field

from . import agent, trace
from .config import INTERNAL_SERVICE_TOKEN, ORIGINATION_URL
from .llm_client import (
    LLMCostGuardError,
    LLMInsufficientDataError,
    LLMResponseError,
    LLMTimeoutError,
    summarize_application,
)
from .policy_chat import PolicyChatResponseError, answer_policy_question
from .schemas import PolicyAnswer

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("loan-assistant")

app = FastAPI(title="Meridian Loan Assistant", version="1.0.0")
# W7: GET /metrics in Prometheus text format -- see gateway/app/main.py's
# comment for why this exists across all 8 services now.
Instrumentator().instrument(app).expose(app)

_FETCH_TIMEOUT = 10.0


# Same catch-all pattern as decision-service and gateway -- this service never
# had one before (review-pass finding), meaning any exception not explicitly
# enumerated in a route below fell through to FastAPI's default behavior
# instead of a controlled response.
@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.error("unhandled error on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "internal error"})


@app.get("/health")
def health():
    return {"status": "ok", "service": "loan-assistant"}


@app.post("/applications/{app_id}/summary")
def summarize(app_id: int, x_user_role: str | None = Header(default=None, alias="X-User-Role")):
    # The gateway only proxies here for csr/underwriter/admin sessions (see
    # gateway/app/main.py assistant()) and forwards the resolved role as this
    # header; pass it through so origination-service will release financials.
    #
    # Bug fix: origination-service's financials route requires X-Internal-Token
    # too, not just a staff X-User-Role (review fix closing a role-spoofing
    # gap) -- this call never sent it, so every summary request 403'd
    # regardless of caller role. See config.py's INTERNAL_SERVICE_TOKEN.
    #
    # Security fix (PR #6 review): GET /applications/{app_id} on
    # origination-service is now staff-only (same reasoning as the
    # /financials call just below) -- this call used to send neither header
    # at all, only /financials did. Send both here too, or every summary
    # request now 403s.
    # One trace per request, opened at THIS SERVICE's ingress -- which is one
    # hop downstream of the gateway, where the session is actually resolved and
    # the staff check happens. Deliberately not called gateway entry; see
    # app/trace.py. Carries the caller's ROLE, never their identity: role is
    # what explains an authorisation outcome, who they are is on the
    # prohibited list.
    with trace.summary_trace(role=x_user_role):
        # Every exit records an outcome, including the ones that never reach
        # the agent. Found in review: a 404, a 403 or an unreachable upstream
        # emitted a trace containing only the `request` stage, which reads as a
        # request that vanished rather than one that was answered -- the exact
        # ambiguity a trace exists to remove.
        try:
            result = _summarize(app_id, x_user_role)
        except HTTPException as exc:
            trace.record("outcome", outcome="refused", status="refused",
                         http_status=exc.status_code,
                         refusal_class=_UPSTREAM_REFUSALS.get(exc.status_code, "none"))
            raise
        except Exception:
            trace.record("outcome", outcome="error", status="error",
                         http_status=500, refusal_class="none")
            raise
        return result


#: Upstream failures reaching the route before the agent runs. Categorical, and
#: mapped from the status rather than the detail string, which carries text.
_UPSTREAM_REFUSALS = {
    404: "application_not_found",
    403: "forbidden",
    502: "upstream_unavailable",
}


def _summarize(app_id: int, x_user_role: str | None):
    main_headers = {"X-Internal-Token": INTERNAL_SERVICE_TOKEN}
    if x_user_role:
        main_headers["X-User-Role"] = x_user_role
    try:
        resp = httpx.get(
            f"{ORIGINATION_URL}/applications/{app_id}", headers=main_headers, timeout=_FETCH_TIMEOUT
        )
    except httpx.HTTPError as exc:
        log.error("origination-service unreachable app_id=%s: %s", app_id, exc)
        raise HTTPException(status_code=502, detail="origination-service unreachable") from exc

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="application not found")
    resp.raise_for_status()
    app_data = resp.json()

    try:
        fin_headers = {"X-Internal-Token": INTERNAL_SERVICE_TOKEN}
        if x_user_role:
            fin_headers["X-User-Role"] = x_user_role
        fin_resp = httpx.get(
            f"{ORIGINATION_URL}/applications/{app_id}/financials",
            headers=fin_headers,
            timeout=_FETCH_TIMEOUT,
        )
    except httpx.HTTPError as exc:
        log.error("origination-service unreachable app_id=%s: %s", app_id, exc)
        raise HTTPException(status_code=502, detail="origination-service unreachable") from exc

    if fin_resp.status_code == 403:
        raise HTTPException(status_code=403, detail="staff only")
    fin_resp.raise_for_status()
    app_data.update(fin_resp.json())

    try:
        summary = summarize_application(app_data)
    except LLMInsufficientDataError as exc:
        trace.record("outcome", outcome="refused", status="refused",
                     http_status=422, refusal_class="LLMInsufficientDataError")
        # The reader gets `exc.detail`; the log keeps the field names and the
        # app_id. A 422 body rendered straight into the UI is read by a loan
        # officer, not by whoever wrote the guard.
        log.info("summary refused: %s", exc)
        raise HTTPException(status_code=422, detail=exc.detail) from exc
    except LLMCostGuardError as exc:
        trace.record("outcome", outcome="refused", status="refused",
                     http_status=400, refusal_class="LLMCostGuardError")
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LLMTimeoutError as exc:
        trace.record("outcome", outcome="refused", status="refused",
                     http_status=504, refusal_class="LLMTimeoutError")
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except LLMResponseError as exc:
        trace.record("outcome", outcome="refused", status="refused",
                     http_status=502, refusal_class="LLMResponseError")
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    # The agent's refusals are designed behaviour and must render as the
    # contract, not as an internal error. Before this block every one of them
    # fell through to the catch-all above and came back as 500 {"detail":
    # "internal error"} -- so a skipped tool call, a retrieval miss, a missing
    # Bedrock configuration and a loop-budget breach were indistinguishable from
    # a crash, both to the officer and to whoever was on call. Found in review
    # on PR #63.
    #
    # Ordered specific-first. `AgentError` last is the point of the base class:
    # a refusal added later still gets a controlled status instead of silently
    # regressing to 500.
    except agent.AgentTimeout as exc:
        trace.record("outcome", outcome="refused", status="refused",
                     http_status=504, refusal_class=type(exc).__name__)
        # 504 is the status `call_api` used to produce for a slow model. Keeping
        # it means the timeout contract survived the move to the agent.
        log.warning("summary timed out app_id=%s", app_id)
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except (agent.AgentUnavailable, agent.UnsafeTracingConfiguration) as exc:
        trace.record("outcome", outcome="refused", status="refused",
                     http_status=503, refusal_class=type(exc).__name__)
        # Configuration, not a bad answer: the service cannot run the summary at
        # all in its current setup, which is 503 rather than 502.
        log.error("summary unavailable app_id=%s reason=%s", app_id, type(exc).__name__)
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except agent.AgentError as exc:
        trace.record("outcome", outcome="refused", status="refused",
                     http_status=502, refusal_class=type(exc).__name__)
        # RequiredToolNotCalled, PolicyEvidenceMissing, AgentStepBudgetExceeded,
        # AgentProviderError -- all "the upstream model did not give us
        # something we can publish", which is the same 502 LLMResponseError uses.
        log.error("summary refused app_id=%s reason=%s", app_id, type(exc).__name__)
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    trace.record("outcome", outcome="summary_returned", status="ok",
                 http_status=200, refusal_class="none")
    return summary.model_dump()


class PolicyChatIn(BaseModel):
    # Coarse pre-filter (422) before the request even reaches answer_policy_question() --
    # the real cost guard is the MAX_INPUT_TOKENS check run there against the actual
    # system prompt + retrieved excerpt, this just rejects obviously-abusive payloads
    # (e.g. a multi-MB "question") for free at the schema layer.
    question: str = Field(min_length=1, max_length=4000)


@app.post("/policy-chat", response_model=PolicyAnswer)
def policy_chat(body: PolicyChatIn):
    # Gateway only proxies /assistant/* for csr/underwriter/admin sessions
    # (gateway/app/main.py assistant()) -- no per-request role check needed
    # here, this doesn't touch per-applicant financials the way /summary does.
    #
    # Same llm_client exception -> HTTP status mapping as summarize() above --
    # answer_policy_question() calls the same guardrailed call_api(), so it can
    # raise the same LLMTimeoutError on a slow/failed Bedrock or Anthropic call.
    try:
        return answer_policy_question(body.question)
    except LLMCostGuardError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LLMTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except LLMResponseError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except PolicyChatResponseError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
