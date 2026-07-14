"""Decision service — FastAPI.

Standalone credit-decisioning service, extracted from the origination service (LOS).
Exposes the synchronous decisioning chain (bureau pull + rules scorecard) and persists
the bare outcome to the shared `decisions` table. The decisioning write path uses raw
psycopg2 — the same partial, unfinished ORM migration seam as origination.
"""
import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import ALLOW_CREDIT_STUB, ENVIRONMENT, EXPERIAN_KEY
from .logging_config import get_logger
from .routers import decisions

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = get_logger("decision-service")

app = FastAPI(title="Meridian Decision Service", version="2.0.0")
app.include_router(decisions.router)

# Outside dev/test, a missing EXPERIAN_KEY means every /decisions call will raise
# CreditBureauUnavailableError -- surface that at readiness time instead of letting
# the stack report healthy and fail on the first real request.
_CREDIT_BUREAU_MISCONFIGURED = not ALLOW_CREDIT_STUB and not EXPERIAN_KEY
if _CREDIT_BUREAU_MISCONFIGURED:
    log.error(
        "EXPERIAN_KEY is not set and ENVIRONMENT=%r does not allow the dev stub -- "
        "/decisions will fail on every request; reporting unhealthy", ENVIRONMENT,
    )


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.error("unhandled error on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "internal error"})


@app.get("/health")
def health():
    if _CREDIT_BUREAU_MISCONFIGURED:
        return JSONResponse(status_code=503, content={
            "status": "unhealthy",
            "service": "decision-service",
            "reason": "EXPERIAN_KEY not set and dev stub not allowed",
        })
    return {"status": "ok", "service": "decision-service"}
