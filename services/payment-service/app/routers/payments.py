"""Payment capture API. POST /payments charges a card/ACH and applies it to the balance."""
import secrets
from fastapi import APIRouter, Header, HTTPException

from .. import config, payments, reconcile
from ..payments import IdempotencyKeyConflict, ServicingAuthUnavailable
from ..schemas import PaymentIn, PaymentOut

router = APIRouter(tags=["payments"])


def _require_internal_token(x_internal_token: str | None) -> None:
    # Defense in depth: the network boundary (no host port -- see
    # docker-compose.yml) is the primary control; this is the fallback in case
    # that boundary is ever mistakenly reopened. An unset config token can
    # never match, so a deploy that forgets to set one fails closed.
    if (not config.INTERNAL_SERVICE_TOKEN or not x_internal_token
            or not secrets.compare_digest(x_internal_token, config.INTERNAL_SERVICE_TOKEN)):
        raise HTTPException(status_code=401, detail="not authorized")


@router.get("/payments/unreconciled")
def get_unreconciled(
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
):
    """Money captured on the card but not yet credited to a loan balance.

    PR #8 review: these rows existed before but nothing could list them --
    `applied_at IS NULL` was queried nowhere, so an operator had no way to find
    out that a borrower had been charged without their balance moving. Gated
    like every other route here: the counts alone say how much money is in
    limbo, which is not something an anonymous caller should be able to read.
    """
    _require_internal_token(x_internal_token)
    return reconcile.unreconciled_summary()


@router.post("/payments/reconcile")
def post_reconcile(
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
):
    """Run one reconciliation pass now, instead of waiting for the next poll.

    Exists so the drain is operable by hand during an incident, and so a
    deployment that disables the in-process worker
    (PAYMENT_RECONCILE_INTERVAL_SECONDS=0) can still trigger it from a
    scheduled job. Safe to call concurrently -- see reconcile.claim_due.
    """
    _require_internal_token(x_internal_token)
    return reconcile.reconcile_once(config.RECONCILE_BATCH_SIZE)


@router.post("/payments", response_model=PaymentOut)
def post_payment(
    body: PaymentIn,
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
):
    _require_internal_token(x_internal_token)

    try:
        return payments.charge(
            body.loan_id, body.processor_token, body.last4, body.amount, body.idempotency_key,
            body.brand, body.name, body.method, body.source_ref,
        )
    except IdempotencyKeyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ServicingAuthUnavailable:
        # 503, not 500: the card was NOT charged, and retrying once the token
        # skew is corrected is the right thing for the caller to do. Reported as
        # a payments outage rather than as a client error, because nothing about
        # the request was wrong (review round 2).
        raise HTTPException(
            status_code=503,
            detail="payments are temporarily unavailable; no charge was made",
        )
