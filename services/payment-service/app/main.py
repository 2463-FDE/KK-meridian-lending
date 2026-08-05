"""Payment service — FastAPI.

Standalone card/ACH charge capture extracted from servicing-service. Card data is
tokenized in the browser (ADR 0008), so this service never receives a raw PAN, CVV or
SSN. A required idempotency_key + the amount range check below close the double-charge
(D2) and negative/NaN-amount gaps; app/reconcile.py drains captures that were authorized
but never applied to a loan balance -- see payments.py for the current charge() flow.
"""
import asyncio
import logging
import math
import os

from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from prometheus_client import Gauge
from prometheus_fastapi_instrumentator import Instrumentator

from . import config, reconcile
from .logging_config import get_logger
from .routers import payments

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = get_logger("payment-service")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Own the reconciler task for the application's lifetime.

    Review fix: the first version of this called `asyncio.create_task(...)` and
    discarded the handle. The event loop holds only a WEAK reference to a
    running task, so a task nobody else references can be garbage-collected
    mid-await -- the drain would then stop silently, and the symptom would be
    money sitting captured-and-uncredited again with nothing raising anywhere.
    Exactly the failure this mechanism exists to prevent.

    So: keep the handle on app.state for the process lifetime, and on shutdown
    cancel and await it, so a pass in flight is not abandoned halfway and the
    loop is really gone before the event loop is torn down.
    """
    task = None
    # 0 disables the in-process worker -- what the test suite uses, and what a
    # deployment running the drain as a separate scheduled job should set.
    if config.RECONCILE_INTERVAL_SECONDS <= 0:
        log.info("in-process payment reconciler disabled (interval=0)")
    else:
        log.info("starting payment reconciler interval=%ss", config.RECONCILE_INTERVAL_SECONDS)
        task = asyncio.create_task(_reconcile_loop(), name="payment-reconciler")
    app.state.reconciler_task = task

    yield

    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    log.info("payment reconciler stopped")


app = FastAPI(title="Meridian Payment Service", version="2.0.0", lifespan=lifespan)
app.include_router(payments.router)
# W7: GET /metrics in Prometheus text format -- see gateway/app/main.py's
# comment for why this exists across all 8 services now.
Instrumentator().instrument(app).expose(app)


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


# --- captured-but-unapplied reconciliation (PR #8 review, high) ---------------
#
# Money captured on the card but never credited to the loan balance used to be
# recoverable only if the client happened to retry the same idempotency_key.
# These two gauges are the alerting surface for what the drain loop below has
# not managed to clear; `payments_unapplied_count` going non-zero and staying
# non-zero is the condition worth paging on.
UNAPPLIED_COUNT = Gauge(
    "payments_unapplied_count",
    "Payments authorized on the card but not yet applied to a loan balance",
)
UNAPPLIED_EXHAUSTED = Gauge(
    "payments_unapplied_exhausted_count",
    "Unapplied payments that have exhausted automatic retries and need manual reconciliation",
)


def _publish_unreconciled_gauges() -> None:
    summary = reconcile.unreconciled_summary()
    UNAPPLIED_COUNT.set(summary["pending"])
    UNAPPLIED_EXHAUSTED.set(summary["exhausted"])


async def _reconcile_loop() -> None:
    """Poll-and-drain. Every failure mode here is caught: a reconciliation pass
    that raises must never take the service down, and the next tick retries."""
    while True:
        await asyncio.sleep(config.RECONCILE_INTERVAL_SECONDS)
        try:
            result = await asyncio.to_thread(reconcile.reconcile_once, config.RECONCILE_BATCH_SIZE)
            if result["claimed"]:
                log.info(
                    "reconciliation pass claimed=%s applied=%s still_pending=%s",
                    result["claimed"], result["applied"], result["still_pending"],
                )
            await asyncio.to_thread(_publish_unreconciled_gauges)
        except Exception as exc:  # noqa: BLE001 -- the loop must survive anything
            log.error("reconciliation pass failed error_type=%s", type(exc).__name__)
