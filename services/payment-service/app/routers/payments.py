"""Payment capture API. POST /payments charges a card/ACH and applies it to the balance."""
from fastapi import APIRouter, Header, HTTPException

from .. import config, payments
from ..payments import IdempotencyKeyConflict
from ..schemas import PaymentIn, PaymentOut

router = APIRouter(tags=["payments"])


def _mask_pan(pan: str | None) -> str | None:
    # Display-only helper. The stored payments row and the payment log keep the FULL PAN
    # and CVV (PCI debt) — masking is never applied to what this service persists.
    if not pan:
        return None
    return "•••• " + pan[-4:]


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
            body.loan_id, body.pan, body.cvv, body.amount, body.idempotency_key,
            body.ssn, body.name, body.method,
        )
    except IdempotencyKeyConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc))
