"""Card-network authorization (ADR 0008 follow-up, review fix).

`payments.py::charge()` used to treat receiving a `processor_token` as proof
the card was actually charged -- the token was only shape/length-checked,
never sent anywhere for real authorization. A borrower could POST any
made-up token and last4, and the code would write a captured payment and
tell servicing-service to reduce their loan balance: real money movement
backed by nothing.

`authorize_charge()` closes that gap with the same fail-closed contract
decision-service already uses for its bureau pull / AI scorer
(`_pull_credit`/`_call_ai_scorer`, `CreditBureauUnavailableError`/
`ModelUnavailableError`): outside development/test, a missing or
unreachable real processor must refuse the charge (`ProcessorUnavailableError`)
rather than silently approve one against a fake authority. An explicit
decline from the processor (or the stub's own decline path) raises
`ChargeDeclinedError` -- a legitimate outcome, not a system failure.

No real payment processor is integrated in this training app (same
"no real vendor behind this stub" situation as the bureau pull before a real
Experian account existed). `_stub_authorize()` is the dev/test stand-in:
`ALLOW_PAYMENT_STUB` gates it exactly like `ALLOW_CREDIT_STUB`/
`ALLOW_MODEL_STUB` gate theirs.
"""
import re

import httpx

from .config import ALLOW_PAYMENT_STUB, ENVIRONMENT, PROCESSOR_API_KEY, PROCESSOR_BASE_URL
from .logging_config import get_logger

log = get_logger("processor")

# Matches frontend/lib/tokenize.ts's own mock token shape exactly -- a token
# that doesn't match this was never issued by the (mock) tokenization step at
# all, the same way a real processor would reject a token it never minted.
_MOCK_TOKEN_RE = re.compile(r"^tok_mock_[0-9a-f-]{36}$", re.IGNORECASE)

# Fixed test-decline amount, same convention as a real processor's published
# test-card numbers: lets the decline path be exercised deterministically,
# with no live processor and no need to special-case a fake token value.
_TEST_DECLINE_AMOUNT = 0.02


class ProcessorUnavailableError(RuntimeError):
    """No real processor is configured/reachable and stub mode isn't allowed."""


class ChargeDeclinedError(RuntimeError):
    """The processor (or the stub standing in for one) declined the charge.

    A legitimate, expected outcome -- not a system failure. Callers must
    treat this as "no money moved," never as "captured."
    """


def _stub_authorize(processor_token: str, amount: float) -> str:
    if amount == _TEST_DECLINE_AMOUNT:
        raise ChargeDeclinedError(f"processor declined: test-decline amount {amount}")
    if not _MOCK_TOKEN_RE.match(processor_token or ""):
        raise ChargeDeclinedError("processor declined: token not recognized")
    return "auth_stub_" + processor_token[-12:]


def authorize_charge(processor_token: str, amount: float) -> str:
    """Confirm a real authorization for exactly `amount` before any payment
    is marked captured or any balance is touched. Returns the processor's
    authorization id on success.

    Raises:
        ChargeDeclinedError: the processor declined the charge.
        ProcessorUnavailableError: no processor is configured/reachable and
            ALLOW_PAYMENT_STUB is not set (i.e. outside development/test).
    """
    if not PROCESSOR_API_KEY:
        if not ALLOW_PAYMENT_STUB:
            raise ProcessorUnavailableError(
                f"PROCESSOR_API_KEY is not set (ENVIRONMENT={ENVIRONMENT!r}) -- refusing "
                "to authorize a charge against a fake processor outside development/test."
            )
        log.warning("PROCESSOR_API_KEY not set -- using deterministic dev/test stub authorization")
        return _stub_authorize(processor_token, amount)

    try:
        resp = httpx.post(
            f"{PROCESSOR_BASE_URL}/charges",
            json={"token": processor_token, "amount": amount},
            headers={"Authorization": f"Bearer {PROCESSOR_API_KEY}"},
            timeout=15.0,
        )
        resp.raise_for_status()
        body = resp.json()
    except ChargeDeclinedError:
        raise
    except Exception as exc:
        if not ALLOW_PAYMENT_STUB:
            raise ProcessorUnavailableError(f"processor call failed: {exc}") from exc
        log.warning("processor call failed (%s) -- falling back to dev/test stub authorization", exc)
        return _stub_authorize(processor_token, amount)

    if not body.get("approved"):
        raise ChargeDeclinedError(f"processor declined: {body.get('reason', 'no reason given')}")
    return body["authorization_id"]
