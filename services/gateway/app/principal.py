"""The gateway's signed statement of *which human* is acting.

`X-Internal-Token` proves a request came from inside the estate. It proves
nothing about the person, because every backend holds the same value -- so a
downstream service reading `X-User-Role` is trusting whichever service last set
it, and any backend with the shared token can set it to `admin`. That is the gap
spec 0002 calls out (REQ-ID-3) and the reason servicing has never been able to
enforce a role of its own: it had no non-forgeable statement to enforce against.

This module mints that statement. The gateway is the only component that resolves
a Redis session into a human, so it is the only component that can honestly say
who is acting -- and it signs the claim with a key no other service holds.

**Asymmetric on purpose.** A shared HMAC secret would let any service holding it
mint a principal, which is the property being removed. With Ed25519 the gateway
holds the private key and everyone else holds only the public one: servicing can
*check* a principal and cannot *forge* one. The verification key being public is
not a weakness, it is the point.

**Short-lived and audience-bound.** An assertion is minted per proxied request,
lives `ASSERTION_TTL_SECONDS`, and names the service it is for. A copy captured
off one hop cannot be replayed at another service, and cannot be replayed at all
for long. It is not a session and must never be treated as one.

The private key never leaves this service. `docker-compose.yml` gives
`PRINCIPAL_SIGNING_KEY` to the gateway alone and `PRINCIPAL_VERIFY_KEY` to
servicing, and `tests/test_principal_signing.py` asserts that split against the
compose file itself, because a key distributed to everything is a shared secret
wearing an asymmetric costume.
"""
import time
import uuid

import jwt
from cryptography.hazmat.primitives import serialization

from .config import (
    ASSERTION_AUDIENCE_SERVICING,
    ASSERTION_ISSUER,
    ASSERTION_TTL_SECONDS,
    PRINCIPAL_SIGNING_KEY,
    PrincipalKeyError,
)

#: EdDSA over Ed25519. Fixed here rather than read from the token, and the
#: verifier pins the same single-item allowlist -- `alg` is an attacker-supplied
#: field, and a verifier that honours it accepts `none` or an HMAC forged with
#: the public key it publishes.
ALGORITHM = "EdDSA"

#: The header the assertion travels in. Stripped from every inbound request by
#: `main.py::_proxy`, exactly like `x-user-*` and `x-internal-token`: it is a
#: claim this gateway makes, never one a client is allowed to assert.
HEADER = "X-Principal-Assertion"


def _private_key():
    """The signing key, or a startup-shaped failure explaining what is missing."""
    try:
        key = serialization.load_pem_private_key(
            PRINCIPAL_SIGNING_KEY.encode("utf-8"), password=None
        )
    except Exception as exc:  # noqa: BLE001 -- any parse failure is the same outcome
        raise PrincipalKeyError(
            "PRINCIPAL_SIGNING_KEY is not a readable PEM private key: "
            f"{type(exc).__name__}. Generate a pair with "
            "`python db/tools/generate_principal_keypair.py`."
        ) from exc
    if key.__class__.__name__ != "Ed25519PrivateKey":
        raise PrincipalKeyError(
            f"PRINCIPAL_SIGNING_KEY is a {key.__class__.__name__}; this signer "
            f"uses Ed25519, and accepting another key type here would silently "
            f"change the algorithm the verifier pins."
        )
    return key


def mint(user: dict, audience: str = ASSERTION_AUDIENCE_SERVICING) -> str:
    """A signed, short-lived assertion naming the human behind this request.

    Every claim comes from the resolved session. Nothing here reads a request
    header: if the caller could influence `sub` or `role`, the signature would be
    authenticating the attacker's own statement (REQ-ID-1, REQ-ID-6).
    """
    now = int(time.time())
    claims = {
        "iss": ASSERTION_ISSUER,
        "aud": audience,
        "sub": str(user["id"]),
        "role": str(user["role"]),
        "iat": now,
        # `nbf` as well as `iat`: a verifier that only checks expiry accepts a
        # token minted for the future, which is how a paused clock becomes an
        # unbounded lifetime.
        "nbf": now,
        "exp": now + ASSERTION_TTL_SECONDS,
        # Not used for replay defence today -- there is no shared store of seen
        # ids -- but it makes two assertions for the same user distinguishable
        # in an audit trail, which `jti`-less tokens are not. Named honestly in
        # the ADR rather than implied to be a nonce check.
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(claims, _private_key(), algorithm=ALGORITHM)
