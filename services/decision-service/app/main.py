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

from . import db
from .config import (
    AI_MODEL_API_KEY,
    ALLOW_CREDIT_STUB,
    ALLOW_MODEL_STUB,
    ENVIRONMENT,
    EXPERIAN_KEY,
)
from .logging_config import get_logger
from .routers import decisions

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = get_logger("decision-service")

app = FastAPI(title="Meridian Decision Service", version="2.0.0")
app.include_router(decisions.router)

# Outside dev/test, a missing EXPERIAN_KEY or AI_MODEL_API_KEY means every
# /decisions call will raise CreditBureauUnavailableError/ModelUnavailableError --
# surface that at readiness time instead of letting the stack report healthy and
# fail on the first real request.
_MISSING_BUREAU_KEY = not ALLOW_CREDIT_STUB and not EXPERIAN_KEY
_MISSING_MODEL_KEY = not ALLOW_MODEL_STUB and not AI_MODEL_API_KEY
_DECISIONING_MISCONFIGURED = _MISSING_BUREAU_KEY or _MISSING_MODEL_KEY
if _DECISIONING_MISCONFIGURED:
    log.error(
        "decisioning misconfigured (ENVIRONMENT=%r, missing_bureau_key=%s, "
        "missing_model_key=%s) -- /decisions will fail on every request; "
        "reporting unhealthy", ENVIRONMENT, _MISSING_BUREAU_KEY, _MISSING_MODEL_KEY,
    )


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.error("unhandled error on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "internal error"})


def _decision_events_ready() -> bool:
    """Live check that the decision_events table actually exists.

    db/init/004_decision_events.sql only runs automatically on a FRESH Postgres
    volume's first boot -- an existing deployment with a persistent volume created
    before Week 3 never gets it (review finding). decide() now requires that insert
    to succeed (app/db.py::transaction()), so a missing table must fail readiness
    up front rather than surface as a 500 on the first real POST /decisions. A
    dedicated, monkeypatchable function (rather than an inline query in health())
    so tests can assert both branches without needing a live Postgres.
    """
    try:
        rows = db.query("SELECT to_regclass('public.decision_events') IS NOT NULL AS exists")
        return bool(rows and rows[0]["exists"])
    except Exception as e:
        log.error("readiness check could not verify decision_events table: %s", e)
        return False


@app.get("/health")
def health():
    if _DECISIONING_MISCONFIGURED:
        missing = []
        if _MISSING_BUREAU_KEY:
            missing.append("EXPERIAN_KEY")
        if _MISSING_MODEL_KEY:
            missing.append("AI_MODEL_API_KEY")
        return JSONResponse(status_code=503, content={
            "status": "unhealthy",
            "service": "decision-service",
            "reason": f"{' and '.join(missing)} not set and dev stub not allowed",
        })
    if not _decision_events_ready():
        return JSONResponse(status_code=503, content={
            "status": "unhealthy",
            "service": "decision-service",
            "reason": (
                "decision_events table is missing -- apply "
                "db/migrations/0004_add_decision_events.sql (a persistent volume "
                "created before Week 3 won't have picked up db/init/"
                "004_decision_events.sql automatically)"
            ),
        })
    return {"status": "ok", "service": "decision-service"}
