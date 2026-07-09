"""Loan Assistant service — FastAPI.

Wraps the Week 1 redactor + guardrailed LLM client behind an HTTP API so the gateway
and frontend can reach it. Fetches application data from origination-service (the
system of record) rather than talking to Postgres directly.
"""
import logging
import os

import httpx
from fastapi import FastAPI, HTTPException

from .config import ORIGINATION_URL
from .llm_client import (
    LLMCostGuardError,
    LLMInsufficientDataError,
    LLMResponseError,
    LLMTimeoutError,
    summarize_application,
)

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("loan-assistant")

app = FastAPI(title="Meridian Loan Assistant", version="1.0.0")

_FETCH_TIMEOUT = 10.0


@app.get("/health")
def health():
    return {"status": "ok", "service": "loan-assistant"}


@app.post("/applications/{app_id}/summary")
def summarize(app_id: int):
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
