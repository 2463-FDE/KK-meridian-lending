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
from pydantic import BaseModel

from .config import ORIGINATION_URL
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
    try:
        resp = httpx.get(f"{ORIGINATION_URL}/applications/{app_id}", timeout=_FETCH_TIMEOUT)
    except httpx.HTTPError as exc:
        log.error("origination-service unreachable app_id=%s: %s", app_id, exc)
        raise HTTPException(status_code=502, detail="origination-service unreachable") from exc

    if resp.status_code == 404:
        raise HTTPException(status_code=404, detail="application not found")
    resp.raise_for_status()
    app_data = resp.json()

    try:
        fin_resp = httpx.get(
            f"{ORIGINATION_URL}/applications/{app_id}/financials",
            headers={"X-User-Role": x_user_role} if x_user_role else {},
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
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except LLMCostGuardError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except LLMTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except LLMResponseError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return summary.model_dump()


class PolicyChatIn(BaseModel):
    question: str


@app.post("/policy-chat", response_model=PolicyAnswer)
def policy_chat(body: PolicyChatIn):
    # Gateway only proxies /assistant/* for csr/underwriter/admin sessions
    # (gateway/app/main.py assistant()) -- no per-request role check needed
    # here, this doesn't touch per-applicant financials the way /summary does.
    #
    # Same llm_client exception -> HTTP status mapping as summarize() above --
    # answer_policy_question() calls the same guardrailed _call_api(), so it can
    # raise the same LLMTimeoutError on a slow/failed Bedrock or Anthropic call.
    try:
        return answer_policy_question(body.question)
    except LLMTimeoutError as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except LLMResponseError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except PolicyChatResponseError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
