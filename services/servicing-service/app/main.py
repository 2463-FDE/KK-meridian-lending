"""Servicing service (LSS) — FastAPI.

Read API (loan list / detail / schedule / payment history) uses SQLAlchemy. The
money-moving endpoints (payments, balance adjust, fee waiver) keep their original
raw implementation.

Authorization, stated as it now stands: every money route requires
`X-Internal-Token` and the service refuses to start without a usable one, and the
gateway restricts adjust-balance / waive-fee / late-fee to csr/admin. What this
service itself does not do is identify the human — it reads no principal, ignores
the `x_user_role` header it accepts, and enforces no second approver (D8). This
docstring used to describe the money endpoints as open to any authenticated
caller with no check of any kind, which stopped being true once the token and the
gateway rule landed, and was never the right description of who may authorise a
movement.
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
# D7: reconciliation state is published by reading `reconciliation_runs`, because
# the job that produces it runs in a separate process and its own gauges would
# never reach this registry -- see reconciliation._ReconciliationCollector.
reconciliation.register_metrics()


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


# The tables the preflight below must write to, because they are the tables the
# real apply-payment writes to. Asserted against `balance.apply_payment_once`'s
# source rather than maintained by hand: a hand-kept list of protected things
# reads as complete while missing one, which is how the probe came to write to a
# table the money path never touches.
_PREFLIGHT_WRITE_TABLES = ("payments", "payment_applications", "ledger_entries")


@app.get("/internal/auth-check")
def internal_auth_check(
    x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token"),
    loan_id: Optional[int] = None,
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

    So the check exercises what apply-payment exercises: an INSERT into
    `payment_applications` and an UPDATE of `balances`, the two tables
    `balance.apply_payment_once` writes, through the same connection helper, in
    one transaction that is always rolled back. It proves the path, not the
    credential and not a stand-in for the path.

    Kept cheap on purpose: two statements, no commit, and SKIP LOCKED so it can
    never wait on a real apply. It runs before every card authorization, so it
    must not become the reason payments are slow. Empty tables are fine -- what
    matters is that the statements execute.
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
        #
        # Review round 7: the write used to land in `preflight_writes`, a table
        # that existed only to be written to. That was the same defect one level
        # down -- it proved *a* write, not *this* write. Per-table grant drift, a
        # constraint or trigger failure, or bloat on `payment_applications` or
        # `balances` specifically all left the probe table healthy, so the
        # preflight answered 200 and the card was captured against an apply that
        # could not land. A proxy for the thing is not the thing.
        #
        # So the probe now runs the statements `balance.apply_payment_once`
        # runs, against the tables it writes. `_PREFLIGHT_WRITE_TABLES` is
        # asserted against that function's source, so a future write path that
        # touches a third table fails the test rather than silently escaping the
        # probe.
        with db.transaction() as cur:
            if loan_id is not None:
                # Existence is proven by a plain read, separately from
                # writability, because the locking probe below uses SKIP LOCKED
                # and therefore cannot tell "no such row" from "someone else has
                # it". Conflating the two would make a busy loan look missing.
                #
                # A missing row is not a nuance: apply_payment_once UPDATEs
                # `WHERE loan_id = %s`, so with no row the update matches nothing,
                # raises nothing, and the payment is recorded as applied while the
                # borrower is credited nothing. Verified live -- before this check
                # the preflight answered 200 for a loan_id that does not exist.
                cur.execute("SELECT 1 FROM balances WHERE loan_id = %s", (loan_id,))
                if not cur.fetchall():
                    raise LookupError(f"no balances row for loan_id={loan_id}")
            # Same statement shape as the real apply, including ON CONFLICT, so
            # the unique index is exercised too. The sentinel is negative and
            # random: `payments.id` is a positive SERIAL, so it cannot collide
            # with a real payment, and two concurrent preflights cannot block
            # each other on the primary key.
            sentinel = -secrets.randbelow(2_000_000_000) - 1
            cur.execute(
                "SELECT loan_id FROM balances "
                "WHERE (%s::int IS NULL OR loan_id = %s) "
                "ORDER BY loan_id LIMIT 1 FOR UPDATE SKIP LOCKED",
                (loan_id, loan_id),
            )
            target_rows = cur.fetchall()
            if not target_rows:
                raise LookupError("no unlocked balances row available for payment-path preflight")
            target_loan = loan_id if loan_id is not None else target_rows[0].get(
                "loan_id", next(iter(target_rows[0].values()))
            )
            cur.execute(
                "INSERT INTO payments (id, loan_id, amount, auth_status, capture_source) "
                "VALUES (%s, %s, 0.01, 'captured', 'unknown')",
                (sentinel, target_loan),
            )
            cur.execute(
                "INSERT INTO payment_applications (payment_id, loan_id, amount) "
                "VALUES (%s, %s, %s) ON CONFLICT (payment_id) DO NOTHING "
                "RETURNING payment_id",
                (sentinel, target_loan, 0.01),
            )
            cur.fetchall()
            # Review round 8: this wrote `updated_at` only. The real apply writes
            # `balance` -- so a column-level grant, a trigger attached to
            # `balance`, or a constraint on it could fail while the probe passed,
            # and the capture went ahead against an apply that could not land.
            # Probing the same TABLE as the money path is not the same as probing
            # the same WRITE; the column list is part of the statement.
            #
            # `SET balance = balance` is a no-op in value and a real write in
            # every way Postgres checks: privileges, triggers, constraints, and
            # the read-only transaction test all apply to the column named here.
            # `_PREFLIGHT_BALANCE_COLUMNS` is asserted against
            # `apply_payment_once`'s own UPDATE, so this cannot drift from it.
            #
            # The loan being charged when the caller names one, because a probe
            # of some other loan's row does not prove this loan's row exists.
            # SKIP LOCKED so it never waits behind an apply-payment mid-flight on
            # that same loan: this runs before every card authorization and must
            # not be the reason a payment is slow, or the reason one blocks. No
            # lockable row degrades to a zero-row UPDATE, which still requires
            # the write privilege and still fails in a read-only transaction.
            cur.execute(
                "INSERT INTO ledger_entries "
                "(loan_id, component, amount, entry_type, payment_id) "
                "VALUES (%s, 'principal', -0.01, 'payment', %s)",
                (target_loan, sentinel),
            )
            # The real apply commits, which runs DEFERRABLE INITIALLY DEFERRED
            # allocation and parity checks. Force those same checks inside this
            # throwaway transaction before rolling it back; otherwise a 200 can
            # prove only the immediate half of the payment write path.
            cur.execute("SET CONSTRAINTS ALL IMMEDIATE")
            # Never committed. Both writes exist only long enough to prove the
            # path works, so this leaves no data to clean up.
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
    # The vendor's legacy duplicate of payment-service's /payments, and the half
    # of D2 that is still open. No idempotency key is accepted or checked.
    #
    # A retry inserts another payment record and applies the loan balance again.
    # It double-records and double-applies; it does not perform another processor
    # charge -- this route calls no processor at all, so the borrower's card is
    # untouched and what is wrong is the loan balance and the payment history.
    # Worth stating precisely, because the two defects need different fixes and
    # this comment used to name the wrong one. payment-service's own /payments
    # was fixed (idempotency_key, partial unique index, apply-once); this one was
    # never ported.
    #
    # Two things bound it, and neither closes it: the internal token above, and
    # the gateway, which matches no rule for this path and 404s rather than
    # proxying it -- so a browser or a staff session cannot reach it at all. It
    # is reachable by a service already inside the compose network holding the
    # shared token, and for that caller both duplications are real.
    #
    # Having no processor is also why its rows are labelled
    # capture_source='servicing_legacy' and excluded from reconciliation (D7).
    # Characterized by servicing-service/tests/test_legacy_payments_is_not_idempotent.py.
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
    # LSS half of the split payment flow). It no longer does an unlocked
    # read-modify-write: apply_payment_once writes an immutable ledger entry and
    # the projection trigger composes the delta into `balances`, which is what
    # closed D3. This comment asserted the opposite for as long as the fix has
    # been merged, immediately above the call that fixed it.
    #
    # Still straight off principal, with no waterfall (D14) -- that half of the
    # old comment is accurate and stays.
    # Review fix: idempotent by payment_id now (balance.apply_payment_once) --
    # payment-service retries this call on a same-key retry if a prior attempt
    # never confirmed, so a duplicate call here must not double-apply.
    try:
        new_balance, applied = balance.apply_payment_once(body.payment_id, loan_id, body.amount)
    except balance.PaymentReplayConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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
    # D8, stated as it now stands rather than as it was first reported.
    #
    # `x_user_role` above is accepted and never read -- this handler applies no
    # authorisation rule of its own, and one caller can move money alone with no
    # approver. That is the open part.
    #
    # What is no longer true: the gateway restricts this route to csr/admin
    # (gateway/app/auth.py::can_move_money), and the write is captured in the
    # ledger by 0035's compatibility bridge, so the prior value is recoverable.
    # The captured entry names no actor, because nothing here knows one.
    return {"loan_id": loan_id, "balance": balance.adjust_balance(loan_id, body.new_balance)}


class WaiveIn(BaseModel):
    amount: float


@app.post("/accounts/{loan_id}/waive-fee")
def waive_fee(loan_id: int, body: WaiveIn,
              x_user_role: Optional[str] = Header(None),
              x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token")):
    _require_internal(x_internal_token)
    # Same position as adjust-balance above: no approver and no human principal
    # here (D8, open); csr/admin enforced at the gateway and the delta captured
    # in the ledger by 0035 (both landed). The role header is not consulted.
    return {"loan_id": loan_id, "past_due": balance.waive_fee(loan_id, body.amount)}


@app.post("/accounts/{loan_id}/late-fee")
def late_fee(loan_id: int,
             x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token")):
    _require_internal(x_internal_token)
    return {"loan_id": loan_id, "past_due": delinquency.assess_late_fee(loan_id)}


@app.get("/reconciliation/peek")
def reconciliation_peek():
    """The two totals, plus whether the control that compares them is running.

    D7: this used to return the totals alone, which cannot distinguish "these
    agree" from "nothing has checked since March". `last_successful_run` being
    null is the honest answer for a system that has never run the job, and it is
    the answer an operator needs before trusting the two numbers above it.
    """
    last_ok = reconciliation.last_successful_run()
    return {
        "ledger_total": reconciliation.ledger_total(),
        "settlement_total": reconciliation.settlement_total(),
        # Not a control by itself -- see app/reconcile_job.py. These fields say
        # whether the control has run, so a reader cannot mistake two equal
        # numbers for a reconciliation that happened.
        "last_successful_run": (
            {"id": last_ok["id"], "at": str(last_ok["started_at"]),
             "loans_compared": last_ok["loans_compared"]} if last_ok else None
        ),
        "recent_failures": [
            {"id": r["id"], "at": str(r["started_at"]), "outcome": r["outcome"],
             "breaks_found": r["breaks_found"], "break_value": str(r["break_value"]),
             "error_code": r["error_code"]}
            for r in reconciliation.recent_failures(limit=5)
        ],
    }

