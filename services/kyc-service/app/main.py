"""KYC service — FastAPI.

Runs Customer Identification Program (CIP) verification only and persists a kyc_checks
row. The CIP logic was extracted from origination-service; the gap is carried forward —
no OFAC/sanctions screening, no beneficial-owner (UBO) capture, no ongoing monitoring, no
SAR path. (debt D11)
"""
import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from . import config
from .logging_config import get_logger
from .routers import kyc

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = get_logger("kyc")

# Fail at boot rather than per-request if the internal token is unusable
# (PR #18 review). Import-time so an unusable deployment never serves traffic.
config.validate_internal_token()

app = FastAPI(title="Meridian KYC Service", version="2.0.0")
app.include_router(kyc.router)
# W7: GET /metrics in Prometheus text format -- see gateway/app/main.py's
# comment for why this exists across all 8 services now.
Instrumentator().instrument(app).expose(app)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.error("unhandled error on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "internal error"})


@app.get("/health")
def health():
    return {"status": "ok", "service": "kyc-service"}
