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

Review fix (double-charge on retry): `authorize_charge()` used to be called
with no idempotency key at all -- a same-key retry from payments.py (e.g. the
process dying between the processor approving the charge and payments.py
persisting that fact) had no way to avoid re-issuing a brand new charge at
the processor. `idempotency_key` is now forwarded to the real processor call
(as an `Idempotency-Key` header, the standard convention a real processor
dedupes on) and to the stub, and `get_authorization()` lets a caller ask
"does the processor already have a record of this key?" *before* charging
again -- see payments.py::charge()'s pending-retry branch.
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


# Review fix: stands in for a real processor's own idempotency-key store --
# a real processor (Stripe et al.) remembers an authorization it already
# issued for a given key and returns that SAME authorization on a repeat
# call instead of charging again. Keyed on idempotency_key, not token/amount,
# since a key is the thing a retry is guaranteed to repeat.
_stub_authorizations: dict[str, str] = {}


def _stub_authorize(processor_token: str, amount: float, idempotency_key: str = None) -> str:
    if idempotency_key and idempotency_key in _stub_authorizations:
        return _stub_authorizations[idempotency_key]
    if amount == _TEST_DECLINE_AMOUNT:
        raise ChargeDeclinedError(f"processor declined: test-decline amount {amount}")
    if not _MOCK_TOKEN_RE.match(processor_token or ""):
        raise ChargeDeclinedError("processor declined: token not recognized")
    auth_id = "auth_stub_" + processor_token[-12:]
    if idempotency_key:
        _stub_authorizations[idempotency_key] = auth_id
    return auth_id


def get_authorization(idempotency_key: str) -> str | None:
    """Ask the processor whether it already has an authorization on record
    for this idempotency_key, without issuing a new charge.

    Review fix: a process that dies between the processor approving a charge
    and payments.py persisting `authorization_id`/`auth_status='captured'`
    used to leave a payment stuck 'pending' with no local record that the
    charge already happened -- a retry then called authorize_charge() again,
    a real risk of a second charge at the processor. A pending retry calls
    this FIRST; only if the processor genuinely has no record of the key is
    it safe to call authorize_charge() (see payments.py::charge()).

    Returns None if the processor has no record of this key (never charged,
    or genuinely unreachable in a dev/test stub configuration).
    """
    if not idempotency_key:
        return None
    if not PROCESSOR_API_KEY:
        if not ALLOW_PAYMENT_STUB:
            raise ProcessorUnavailableError(
                f"PROCESSOR_API_KEY is not set (ENVIRONMENT={ENVIRONMENT!r}) -- refusing "
                "to look up an authorization against a fake processor outside development/test."
            )
        return _stub_authorizations.get(idempotency_key)

    try:
        resp = httpx.get(
            f"{PROCESSOR_BASE_URL}/charges",
            params={"idempotency_key": idempotency_key},
            headers={"Authorization": f"Bearer {PROCESSOR_API_KEY}"},
            timeout=15.0,
        )
        resp.raise_for_status()
        body = resp.json()
    except Exception as exc:
        if not ALLOW_PAYMENT_STUB:
            raise ProcessorUnavailableError(f"processor lookup call failed: {exc}") from exc
        log.warning("processor lookup call failed (%s) -- falling back to dev/test stub lookup", exc)
        return _stub_authorizations.get(idempotency_key)

    if not body.get("approved"):
        return None
    return body["authorization_id"]


def authorize_charge(processor_token: str, amount: float, idempotency_key: str = None) -> str:
    """Confirm a real authorization for exactly `amount` before any payment
    is marked captured or any balance is touched. Returns the processor's
    authorization id on success.

    `idempotency_key` (Review fix) is forwarded to the real processor so it
    also dedupes on its end -- a repeat call with the same key returns the
    SAME authorization rather than charging twice.

    Raises:
        ChargeDeclinedError: the processor declined the charge.
        ProcessorUnavailableError: no processor is configured/reachable and
            ALLOW_PAYMENT_STUB is not set (i.e. outside development/test).
    """
    # Review fix: callers pass row["amount"] read back from Postgres, which
    # is a Decimal regardless of what type was inserted (same gap as
    # _apply_via_servicing) -- httpx's json= can't serialize Decimal at all,
    # and Decimal('0.02') != 0.02 under naive comparison broke the stub's own
    # test-decline check. Normalize to float once, up front.
    amount = float(amount)
    if not PROCESSOR_API_KEY:
        if not ALLOW_PAYMENT_STUB:
            raise ProcessorUnavailableError(
                f"PROCESSOR_API_KEY is not set (ENVIRONMENT={ENVIRONMENT!r}) -- refusing "
                "to authorize a charge against a fake processor outside development/test."
            )
        log.warning("PROCESSOR_API_KEY not set -- using deterministic dev/test stub authorization")
        return _stub_authorize(processor_token, amount, idempotency_key)

    try:
        resp = httpx.post(
            f"{PROCESSOR_BASE_URL}/charges",
            json={"token": processor_token, "amount": amount, "idempotency_key": idempotency_key},
            headers={
                "Authorization": f"Bearer {PROCESSOR_API_KEY}",
                **({"Idempotency-Key": idempotency_key} if idempotency_key else {}),
            },
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
        return _stub_authorize(processor_token, amount, idempotency_key)

    if not body.get("approved"):
        raise ChargeDeclinedError(f"processor declined: {body.get('reason', 'no reason given')}")
    return body["authorization_id"]
