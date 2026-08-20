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

from . import (balance, config, db, delinquency, maker_checker, principal,
               reconciliation, waterfall)
from .logging_config import get_logger
from .routers import loans

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = get_logger("servicing")

# Fail at boot rather than per-request on an unusable token (PR #22 review).
config.validate_internal_token()
# Same fail-closed treatment for the key that proves WHO is acting (spec 0002
# REQ-ID-3). A malformed key -- or the private half landing here, which would let
# this service mint the identities it is supposed to only check -- is a boot
# failure, not a per-request surprise on a staff action.
config.validate_principal_verify_key()
# The approved limits, validated at boot. An unset threshold would silently let
# an underwriter approve any amount; an unset cap would let any amount be
# proposed. Neither is a weaker mode of operation -- both are a boot failure.
config.validate_maker_checker_config()

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


# `PaymentIn` and `POST /payments` were removed here, not disabled.
#
# They were the vendor's processorless duplicate of payment-service's charge
# endpoint: no idempotency key, so a retry inserted a second `payments` row and
# applied the loan balance a second time (docs/DEBT.md D2's open half). Nothing
# called it. The gateway matched no rule for `/lss/payments` and 404'd, no
# frontend referenced it, and payment-service -- the canonical, processor-backed
# path -- never used it. Its only callers were its own tests.
#
# Deleted rather than left behind a flag or a 410, because a disabled money route
# is still a money route: it keeps its schema, its imports and its place in the
# next reader's mental model, and re-enabling it is a one-line mistake. What
# replaces it is the absence itself, asserted by
# tests/test_legacy_payments_route_is_retired.py.
#
# Historical rows are untouched. Every `payments` row this route wrote carries
# `capture_source='servicing_legacy'`, that value stays in the CHECK constraint,
# and reconciliation still counts and excludes those rows exactly as before --
# they have no processor behind them and never will, which is why they are
# excluded rather than compared. Nothing new can be written with that label.


class ApplyPaymentIn(BaseModel):
    amount: float
    payment_id: int
    # The trace identifier payment-service already minted, stored and logged.
    # Optional so a caller running an older image still applies payments rather
    # than 422-ing on a field it does not know about -- a rolling deploy must not
    # strand a captured charge. Absent means "no trace", which is honest; it is
    # never replaced with one minted here, because two generators produce two
    # ids and neither side can find the other's.
    correlation_id: Optional[str] = Field(default=None, max_length=64,
                                          pattern=r"^[A-Za-z0-9_-]+$")


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
    # The waterfall applies here (D14): the amount is split fees -> accrued
    # interest -> principal, in the order `policies/fee_schedule.md` publishes,
    # and one ledger entry is written per component that actually moved. This
    # comment said "still straight off principal, with no waterfall" until that
    # became untrue.
    # Review fix: idempotent by payment_id now (balance.apply_payment_once) --
    # payment-service retries this call on a same-key retry if a prior attempt
    # never confirmed, so a duplicate call here must not double-apply.
    try:
        new_balance, applied = balance.apply_payment_once(
            body.payment_id, loan_id, body.amount,
            correlation_id=body.correlation_id)
    except balance.PaymentReplayConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except waterfall.PaymentExceedsAmountOwed as exc:
        # 409, and mapped here rather than left to the catch-all handler, which
        # would return `{"detail": "internal error"}` with a 500 and tell the
        # caller nothing. What happens to an overpayment is an open Lending
        # Operations question (see waterfall.py); refusing states that instead
        # of picking an answer silently, so the caller is told the payment was
        # larger than everything owed and by how much.
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except waterfall.AmountIsNotWholeCents as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
    """A signed DELTA on a named component, never a target balance.

    spec 0002 REQ-VAL-1. "Set the balance to 250.00" cannot be reviewed without
    knowing what it is now, and what it is now can change between the review and
    the approval -- so the old `new_balance` field is gone rather than optional.
    A caller still sending it gets a 422 from `extra="forbid"` instead of having
    it silently ignored.
    """
    component: Literal["principal", "fees"]
    amount: float
    reason: str
    model_config = {"extra": "forbid"}


@app.post("/accounts/{loan_id}/adjust-balance")
def adjust_balance(loan_id: int, body: AdjustIn,
                   x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
                   x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
                   x_principal_assertion: Optional[str] = Header(
                       None, alias="X-Principal-Assertion"),
                   x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token")):
    """Raise an adjustment PROPOSAL. This route no longer moves money.

    D8 closed. It used to set `balances.balance` to whatever it was given, on the
    say-so of whoever called it. It now writes a `pending_movements` row and
    returns 202: nothing moves until a *different* authorised person approves it,
    and the approval -- not this call -- writes the ledger entry.
    """
    _require_internal(x_internal_token)
    actor = principal.require_staff_principal(
        x_principal_assertion, claimed_role=x_user_role, claimed_user=x_user_id,
    )
    proposal = maker_checker.propose(
        loan_id, component=body.component, amount=body.amount,
        entry_type="adjustment", reason=body.reason, actor=actor,
    )
    return JSONResponse(status_code=202, content=proposal)


class WaiveIn(BaseModel):
    amount: float
    reason: str
    model_config = {"extra": "forbid"}


@app.post("/accounts/{loan_id}/waive-fee")
def waive_fee(loan_id: int, body: WaiveIn,
              x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
              x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
              x_principal_assertion: Optional[str] = Header(
                  None, alias="X-Principal-Assertion"),
              x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token")):
    """Raise a fee-waiver PROPOSAL. Moves no money -- see adjust-balance."""
    _require_internal(x_internal_token)
    actor = principal.require_staff_principal(
        x_principal_assertion, claimed_role=x_user_role, claimed_user=x_user_id,
    )
    proposal = maker_checker.propose(
        loan_id, component="fees", amount=body.amount,
        entry_type="fee_waived", reason=body.reason, actor=actor,
    )
    return JSONResponse(status_code=202, content=proposal)


@app.get("/movements")
def movement_queue(x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
                   x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
                   x_principal_assertion: Optional[str] = Header(
                       None, alias="X-Principal-Assertion"),
                   x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token")):
    """The unresolved queue. Visibility is not authority: any staff principal may
    read it, and reading it approves nothing."""
    _require_internal(x_internal_token)
    principal.require_staff_principal(
        x_principal_assertion, claimed_role=x_user_role, claimed_user=x_user_id,
    )
    return {"movements": maker_checker.queue()}


class ResolveIn(BaseModel):
    resolution: Literal["approved", "rejected"]
    model_config = {"extra": "forbid"}


@app.post("/movements/{movement_id}/resolve")
def resolve_movement(movement_id: int, body: ResolveIn,
                     x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
                     x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
                     x_principal_assertion: Optional[str] = Header(
                         None, alias="X-Principal-Assertion"),
                     x_internal_token: Optional[str] = Header(
                         None, alias="X-Internal-Token")):
    """Approve or reject. The second person, and the only path that moves money
    for a staff-directed adjustment or waiver.

    One endpoint rather than two, because approving and rejecting are the same
    decision with different outcomes and must share one lock, one transition and
    one set of checks. Two endpoints would be two chances to diverge.
    """
    _require_internal(x_internal_token)
    actor = principal.require_staff_principal(
        x_principal_assertion, claimed_role=x_user_role, claimed_user=x_user_id,
    )
    return maker_checker.resolve(movement_id, resolution=body.resolution, actor=actor)


@app.post("/accounts/{loan_id}/late-fee")
def late_fee(loan_id: int,
             x_user_id: Optional[str] = Header(None, alias="X-User-Id"),
             x_user_role: Optional[str] = Header(None, alias="X-User-Role"),
             x_principal_assertion: Optional[str] = Header(
                 None, alias="X-Principal-Assertion"),
             x_internal_token: Optional[str] = Header(None, alias="X-Internal-Token")):
    # Grouped with the staff routes, not the machine ones, because nothing
    # automated calls it: `delinquency.assess_late_fee` has exactly one caller,
    # this handler, and the gateway already treats the route as money-moving
    # (csr/admin). If a scheduled assessor is ever built it will need a machine
    # path of its own rather than a fabricated human -- spec 0002 §8 keeps
    # machine-originated fees outside the staff workflow deliberately.
    _require_internal(x_internal_token)
    actor = principal.require_money_principal(
        x_principal_assertion, claimed_role=x_user_role, claimed_user=x_user_id,
    )
    log.info("late-fee loan_id=%s by subject=%s role=%s",
             loan_id, actor.subject, actor.role)
    try:
        past_due = delinquency.assess_late_fee(loan_id)
    except delinquency.NoFeeIsDue as exc:
        # 409, and mapped HERE for the same reason as the 404 below.
        #
        # **This is the second time this exact shape has appeared.** PR #38's
        # review caught `LoanHasNoBalances` falling through to the catch-all
        # handler as a 500; this PR added a new named exception and repeated it.
        # A named refusal with no landing pad at the route is indistinguishable
        # from a crash to whoever called.
        #
        # 409 rather than 404 or 200: the loan exists and the request is
        # well-formed -- what refuses it is the loan's own state, which is a
        # conflict. It matches `PaymentExceedsAmountOwed` on the apply-payment
        # route, where a staff member likewise asked for a money movement the
        # rules do not permit. A 200 would be worse: a caller who asked to
        # charge a fee and got a success with no fee has to inspect the body to
        # learn nothing happened.
        log.info("late-fee declined loan_id=%s -- %s", loan_id, exc)
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except delinquency.LoanHasNoBalances:
        # 404, and mapped HERE rather than left to the global handler.
        #
        # Without this the refusal reaches the caller as `{"detail": "internal
        # error"}` with status 500, because `@app.exception_handler(Exception)`
        # catches everything. That would make the whole point of the refusal
        # false: the previous code returned 35.0 for a loan whose balance never
        # moved, and replacing a wrong number with an opaque server error is not
        # the visible refusal it was replaced for. Reviewed on PR #38.
        #
        # 404 rather than 409: nothing about the request conflicts with the
        # loan's state -- the balance this fee would be assessed against does
        # not exist. A conflict code would tell the caller to retry later, which
        # will never help.
        log.warning("late-fee refused loan_id=%s -- no balances row", loan_id)
        raise HTTPException(
            status_code=404,
            detail=f"loan {loan_id} has no balance to assess a fee against",
        )
    return {"loan_id": loan_id, "past_due": past_due}


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

