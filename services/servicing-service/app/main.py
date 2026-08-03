"""Servicing service (LSS) — FastAPI.

Read API (loan list / detail / schedule / payment history) uses SQLAlchemy. The
money-moving endpoints (payments, balance adjust, fee waiver) keep their original raw
implementation and accept ANY authenticated caller — no role check, no maker-checker.
(weak authz — kept on purpose)
"""
import logging
import os

from fastapi import FastAPI, Header, Request
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel
from typing import Optional

from . import balance, delinquency, payments, reconciliation
from .logging_config import get_logger
from .routers import loans

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = get_logger("servicing")

app = FastAPI(title="Meridian Servicing Service (LSS)", version="2.0.0")
app.include_router(loans.router)
# W7: GET /metrics in Prometheus text format -- see gateway/app/main.py's
# comment for why this exists across all 8 services now.
Instrumentator().instrument(app).expose(app)


@app.exception_handler(Exception)
async def unhandled(request: Request, exc: Exception):
    log.error("unhandled error on %s: %s", request.url.path, exc)
    return JSONResponse(status_code=500, content={"detail": "internal error"})


@app.get("/health")
def health():
    return {"status": "ok", "service": "servicing"}


class PaymentIn(BaseModel):
    # ADR 0008 (Week 5 tokenization): this used to accept raw pan/cvv/ssn
    # directly and log/persist them unredacted (D5) -- the exact same gap
    # payment-service's own /payments closed, just not yet ported to this
    # duplicate, legacy endpoint. Same contract now: only the processor's
    # opaque token plus non-sensitive display fields, never a raw PAN/CVV,
    # and ssn had no functional role in a card/ACH charge here to begin with.
    # `extra="forbid"` makes that a real rejection, not a silent field drop.
    model_config = {"extra": "forbid"}

    loan_id: int
    processor_token: str
    last4: Optional[str] = None
    brand: Optional[str] = None
    amount: float
    name: Optional[str] = None
    method: str = "card"


@app.post("/payments")
def post_payment(body: PaymentIn):
    # No idempotency key accepted or checked. Retried POST = second charge. (debt D2,
    # unrelated to the PCI/D5 fix above -- left as-is, same scope boundary
    # payment-service's own idempotency fix drew.)
    return payments.charge(
        body.loan_id, body.processor_token, body.last4, body.brand, body.amount, body.name, body.method
    )


class ApplyPaymentIn(BaseModel):
    amount: float
    payment_id: int


@app.post("/accounts/{loan_id}/apply-payment")
def apply_payment(loan_id: int, body: ApplyPaymentIn):
    # This is the apply path called by payment-service AFTER it captures the charge (the
    # LSS half of the split payment flow). It still does the unlocked read-modify-write
    # (D3) straight off principal with no waterfall (D14) — preserved exactly as-is.
    # Review fix: idempotent by payment_id now (balance.apply_payment_once) --
    # payment-service retries this call on a same-key retry if a prior attempt
    # never confirmed, so a duplicate call here must not double-apply.
    new_balance, applied = balance.apply_payment_once(body.payment_id, loan_id, body.amount)
    return {
        "loan_id": loan_id,
        "applied_amount": body.amount,
        "new_balance": new_balance,
        "already_applied": not applied,
    }


@app.get("/accounts/{loan_id}/balance")
def get_account_balance(loan_id: int):
    return {
        "loan_id": loan_id,
        "balance": balance.get_balance(loan_id),
        "past_due": balance.get_past_due(loan_id),
    }


class AdjustIn(BaseModel):
    new_balance: float


@app.post("/accounts/{loan_id}/adjust-balance")
def adjust_balance(loan_id: int, body: AdjustIn,
                   x_user_role: Optional[str] = Header(None)):
    # ANY authenticated user. No role check, no second approver, no ledger entry. (debt D8)
    return {"loan_id": loan_id, "balance": balance.adjust_balance(loan_id, body.new_balance)}


class WaiveIn(BaseModel):
    amount: float


@app.post("/accounts/{loan_id}/waive-fee")
def waive_fee(loan_id: int, body: WaiveIn,
              x_user_role: Optional[str] = Header(None)):
    # ANY authenticated user can waive a fee. No maker-checker. (debt D8)
    return {"loan_id": loan_id, "past_due": balance.waive_fee(loan_id, body.amount)}


@app.post("/accounts/{loan_id}/late-fee")
def late_fee(loan_id: int):
    return {"loan_id": loan_id, "past_due": delinquency.assess_late_fee(loan_id)}


@app.get("/reconciliation/peek")
def reconciliation_peek():
    # Not a real control — just exposes the two totals. They don't tie out. (debt D7)
    return {
        "ledger_total": reconciliation.ledger_total(),
        "settlement_total": reconciliation.settlement_total(),
    }
