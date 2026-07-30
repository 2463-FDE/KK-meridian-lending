"""Payment capture API. POST /payments charges a card/ACH and applies it to the balance."""
from fastapi import APIRouter, Header, HTTPException

from .. import config, payments
from ..payments import IdempotencyKeyConflict
from ..schemas import PaymentIn, PaymentOut

router = APIRouter(tags=["payments"])


@router.post("/payments", response_model=PaymentOut)
def post_payment(
    body: PaymentIn,
    x_internal_token: str | None = Header(None, alias="X-Internal-Token"),
):
    # Defense in depth: the network boundary (no host port -- see
    # docker-compose.yml) is the primary control; this is the fallback in case
    # that boundary is ever mistakenly reopened. An unset config token can
    # never match, so a deploy that forgets to set one fails closed.
    if not config.INTERNAL_SERVICE_TOKEN or x_internal_token != config.INTERNAL_SERVICE_TOKEN:
        raise HTTPException(status_code=401, detail="not authorized")

    try:
        return payments.charge(
            body.loan_id, body.processor_token, body.last4, body.amount, body.idempotency_key,
            body.brand, body.name, body.method,
        )
    except IdempotencyKeyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
