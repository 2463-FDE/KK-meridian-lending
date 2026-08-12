"""Servicing service (LSS) — FastAPI.

Read API (loan list / detail / schedule / payment history) uses SQLAlchemy. The
money-moving endpoints (payments, balance adjust, fee waiver) keep their original raw
implementation and accept ANY authenticated caller — no role check, no maker-checker.
(weak authz — kept on purpose)
"""
import logging
import os
import secrets

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, Field
from typing import Literal, Optional

from . import balance, config, db, delinquency, payments, reconciliation
from .logging_config import get_logger
from .routers import loans

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = get_logger("servicing")

# Fail at boot rather than per-request on an unusable token (PR #22 review).
config.validate_internal_token()

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


def _require_internal(x_internal_token: Optional[str]) -> None:
    """Defense in depth on every money-moving route.

    servicing-service publishes no host port, so the network boundary was the
    only thing standing between a caller and these endpoints -- and it was the
    ONLY control, because none of them checks anything itself. That is the same
    position kyc-service was in when it turned out to be reachable anyway,
    first through its own published port and then through an anonymous gateway
    relay that signed requests on the caller's behalf. Both times the topology
    was assumed to be the guarantee, and both times it was not.

    Network topology is not an application-level check. A container that can
    resolve `servicing-service:8002` can move money on any loan: set a balance
    to zero, waive a fee, or post a payment that never happened.

    An unset config token can never match, so a deploy that forgets to set one
    refuses every money-moving call rather than accepting every one -- the same
    fail-closed contract the five sibling services already use.
    """
    expected = config.INTERNAL_SERVICE_TOKEN
    # An unset server-side token can never match -- checked first, because
    # compare_digest("", "") is True and would otherwise admit every caller on a
    # deployment that forgot to configure one. Startup validation should have
    # stopped that already; this is the second line of the same defence.
    if not expected or not x_internal_token:
        raise HTTPException(status_code=401, detail="not authorized")
    # Constant-time: `!=` on str short-circuits at the first differing byte, so
    # response timing leaks how much of the secret a guess got right, one byte at
    # a time. compare_digest does not. The values are ASCII by construction
    # (an env var and an HTTP header), so the str overload is safe here.
    if not secrets.compare_digest(x_internal_token, expected):
        raise HTTPException(status_code=401, detail="not authorized")


@app.get("/internal/auth-check")
def internal_auth_check(
    x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token"),
):
    """Can this service accept AND PERSIST an apply-payment right now?

    That is the contract, and it is deliberately stronger than "our tokens
    match". Review round 5: this used to authenticate and return, touching no
    database at all -- and I documented that as a feature ("no database access,
    no state, so a 200 means exactly the token you sent is the token I expect").
    It was the defect. payment-service reads a 200 as permission to capture a
    card, so a servicing process that is up with its database down answered 200,
    the card was charged, and the follow-up apply-payment failed: a real charge
    with no credit on the loan, which is the outcome the whole preflight exists
    to prevent.

    So the check now exercises the same dependency apply-payment does -- a light
    read against `balances` and `payment_applications`, the two tables
    `balance.apply_payment_once` writes. It proves the path, not just the
    credential.

    Kept cheap on purpose: two LIMIT 1 reads, no writes, no transaction. It runs
    before every card authorization, so it must not become the reason payments
    are slow. Empty tables are fine -- what matters is that the statements
    execute.
    """
    _require_internal(x_internal_token)
    try:
        # A WRITE, rolled back. Two SELECTs were not enough: a read-only replica,
        # a revoked INSERT grant, a read-only transaction or a full disk all let
        # reads pass while apply_payment_once's INSERT INTO payment_applications
        # and UPDATE balances fail -- so a 200 still greenlit a capture that could
        # not be credited. Reads prove reachability; only a write proves the
        # thing this endpoint claims.
        #
        # Same connection helper and therefore the same role and transaction
        # semantics as the real apply, because a preflight on a different
        # connection proves nothing about the one that matters.
        with db.transaction() as cur:
            cur.execute(
                "INSERT INTO preflight_writes (checked_at) VALUES (now()) RETURNING id"
            )
            cur.fetchall()
            # Never committed. The row exists only long enough to prove the
            # write path works, so this leaves no data to clean up and no
            # sequence contention on a real table.
            cur.execute("ROLLBACK")
    except Exception as e:  # noqa
        log.error(
            "auth-check could not complete a write against the apply-payment path "
            "(%s) -- reporting unavailable so no card is captured that we could "
            "not credit",
            type(e).__name__,
        )
        raise HTTPException(
            status_code=503,
            detail="servicing cannot persist an apply-payment right now",
        )
    return {"status": "ok", "auth": "ok"}


class PaymentIn(BaseModel):
    # ADR 0008 (Week 5 tokenization): this used to accept raw pan/cvv/ssn
    # directly and log/persist them unredacted (D5) -- the exact same gap
    # payment-service's own /payments closed, just not yet ported to this
    # duplicate, legacy endpoint. Same contract now: only the processor's
    # opaque token plus non-sensitive display fields, never a raw PAN/CVV,
    # and ssn had no functional role in a card/ACH charge here to begin with.
    # `extra="forbid"` makes that a real rejection, not a silent field drop.
    model_config = {"extra": "forbid"}

    # Bounded, because charge() writes it into the log line BEFORE the insert
    # that would reject a nonexistent loan -- so an unbounded integer is a
    # channel too: `{"loan_id": 4111111111111111}` wrote a raw PAN to
    # payment-service.log even though the charge then failed. `loans.id` is a
    # SERIAL, i.e. int4, so this is the range the column can actually hold and
    # nothing legitimate is refused. Reviewed on PR #16.
    loan_id: int = Field(ge=1, le=2_147_483_647)
    processor_token: str
    # Shape-constrained, not merely name-constrained. `extra="forbid"` rejects
    # unknown FIELD NAMES and says nothing about values, so an unconstrained
    # string field is a channel for exactly the data this endpoint is supposed
    # to have stopped accepting: `method="4111111111111111"` reached
    # `payments.charge()` and was written verbatim to payment-service.log,
    # which made the module's "no card data reaches this logger" claim false.
    # Reviewed on PR #16.
    #
    # Each of the three display fields is now the shape it is documented to be,
    # so a PAN cannot be smuggled through any of them and the 422 names the
    # field rather than dropping it silently.
    last4: Optional[str] = Field(default=None, pattern=r"^\d{4}$")
    brand: Optional[str] = Field(default=None, pattern=r"^[A-Za-z][A-Za-z ]{0,19}$")
    # Also logged, so also bounded. A consumer instalment payment has no
    # business being a sixteen-digit figure, and an unbounded float carries one
    # just as well as a string does.
    amount: float = Field(gt=0, le=10_000_000)
    name: Optional[str] = None
    method: Literal["card", "ach"] = "card"


@app.post("/payments")
def post_payment(body: PaymentIn,
                 x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token")):
    _require_internal(x_internal_token)
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
def apply_payment(loan_id: int, body: ApplyPaymentIn,
                  x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token")):
    _require_internal(x_internal_token)
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
                   x_user_role: Optional[str] = Header(None),
                   x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token")):
    _require_internal(x_internal_token)
    # ANY authenticated user. No role check, no second approver, no ledger entry. (debt D8)
    return {"loan_id": loan_id, "balance": balance.adjust_balance(loan_id, body.new_balance)}


class WaiveIn(BaseModel):
    amount: float


@app.post("/accounts/{loan_id}/waive-fee")
def waive_fee(loan_id: int, body: WaiveIn,
              x_user_role: Optional[str] = Header(None),
              x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token")):
    _require_internal(x_internal_token)
    # ANY authenticated user can waive a fee. No maker-checker. (debt D8)
    return {"loan_id": loan_id, "past_due": balance.waive_fee(loan_id, body.amount)}


@app.post("/accounts/{loan_id}/late-fee")
def late_fee(loan_id: int,
             x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token")):
    _require_internal(x_internal_token)
    return {"loan_id": loan_id, "past_due": delinquency.assess_late_fee(loan_id)}


@app.get("/reconciliation/peek")
def reconciliation_peek():
    # Not a real control — just exposes the two totals. They don't tie out. (debt D7)
    return {
        "ledger_total": reconciliation.ledger_total(),
        "settlement_total": reconciliation.settlement_total(),
    }
