"""Origination service (LOS) — FastAPI.

Endpoints: application intake, KYC (CIP), decisioning, and offer/disclosure. Read
paths (list/detail) use SQLAlchemy; the older write paths (intake, decisioning,
boarding) still use raw psycopg2 — a partial, unfinished migration.
"""
import logging
import os

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator

from . import config
from .logging_config import get_logger
from .routers import applications, offers

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = get_logger("origination")

# Fail at boot rather than per-request if the internal token is unusable
# (PR #18 review). Import-time so an unusable deployment never serves traffic.
config.validate_internal_token()

app = FastAPI(title="Meridian Origination Service (LOS)", version="2.0.0")
app.include_router(applications.router)
app.include_router(offers.router)
# W7: GET /metrics in Prometheus text format -- see gateway/app/main.py's
# comment for why this exists across all 8 services now.
Instrumentator().instrument(app).expose(app)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.error("unhandled error on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "internal error"})


@app.get("/health")
def health():
    return {"status": "ok", "service": "origination"}


# Boarding has no route of its own: POST /applications/{app_id}/accept
# (routers/applications.py accept_offer) is the supported atomic path.
