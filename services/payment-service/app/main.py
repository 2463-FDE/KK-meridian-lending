"""Payment service — FastAPI.

Standalone card/ACH charge capture extracted from servicing-service. Stores the full PAN
and CVV on the payments row (D5, D13 — still open, kept on purpose). A required
idempotency_key + the amount range check below close the double-charge (D2) and
negative/NaN-amount gaps -- see payments.py for the current charge() flow.
"""
import logging
import math
import os

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .logging_config import get_logger
from .routers import payments

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = get_logger("payment-service")

app = FastAPI(title="Meridian Payment Service", version="2.0.0")
app.include_router(payments.router)


def _sanitize_non_finite(obj):
    # A rejected NaN/Infinity amount gets echoed back in the 422 body's own
    # "input" field (FastAPI includes the offending value in each error) --
    # Starlette's JSONResponse renders with allow_nan=False, so leaving a raw
    # NaN/Infinity float in there would crash the error response itself with
    # a ValueError instead of returning the 422.
    if isinstance(obj, float) and not math.isfinite(obj):
        return str(obj)
    if isinstance(obj, dict):
        return {k: _sanitize_non_finite(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_non_finite(v) for v in obj]
    return obj


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=422, content={"detail": _sanitize_non_finite(exc.errors())})


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.error("unhandled error on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "internal error"})


@app.get("/health")
def health():
    return {"status": "ok", "service": "payment-service"}
