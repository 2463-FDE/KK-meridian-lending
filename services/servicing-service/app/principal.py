"""Verify *which human* is acting, without being able to invent one.

This is the half of spec 0002's identity boundary that servicing owns, and the
reason D8's role gap could not be closed before it existed.

Until now this service read `x_user_role` and ignored it -- correctly, because
believing it would have been worse than ignoring it. Every backend holds the same
`X-Internal-Token`, so a header stamped by "a service" is a header any service
can stamp: reading it would have let payment-service, kyc-service, or anything
else with the shared token call itself an admin and move a balance. The role rule
therefore lived at the gateway, one hop away from the money, and a caller that
reached servicing directly on the compose network was subject to no role rule at
all.

What changes here: the gateway signs a short-lived, audience-bound assertion with
a key **only it holds** (`gateway/app/principal.py`), and this module verifies it
with the public half. Servicing can now check who is acting and still cannot
fabricate it -- which is what makes an independent role check meaningful rather
than decorative.

**The trust boundary, stated exactly**

  * the shared token authenticates a *service*, never a human (REQ-ID-7);
  * identity and role come only from the verified assertion -- `X-User-Id`,
    `X-User-Role`, body fields and query parameters are untrusted hints, and a
    hint that disagrees with the assertion is refused rather than reconciled
    (REQ-ID-8);
  * every failure is a refusal: missing, malformed, expired, not-yet-valid,
    wrong issuer, wrong audience, wrong algorithm, or an unparseable key all fail
    closed with no fallback to headers or to the token (REQ-ID-9).

**What this does NOT do.** It answers "who is acting, and may they act alone?" It
does not answer "should anyone act alone?" -- that is maker-checker (D8's second
half, spec 0002 §2), which is not implemented. A csr with a valid assertion can
still adjust a balance by themselves; what has changed is that servicing knows it
is a csr, and that nothing on the network can claim to be one.
"""
import time
from dataclasses import dataclass

import jwt
from cryptography.hazmat.primitives import serialization
from fastapi import HTTPException

from . import config
from .logging_config import get_logger

log = get_logger("principal")

#: Pinned to one algorithm, as a one-item allowlist. `alg` is attacker-supplied:
#: a verifier that honours it accepts `none`, and one that allows an HMAC family
#: accepts a token forged with the *public* key as the shared secret -- the
#: classic confusion attack, and a real one here because the public key is
#: distributed by design.
ALGORITHMS = ["EdDSA"]

#: Clock skew allowance. Small deliberately: the assertion lives two minutes, so
#: a generous leeway would be a meaningful fraction of its lifetime.
LEEWAY_SECONDS = 10

HEADER = "X-Principal-Assertion"

#: Roles permitted to move money, enforced HERE rather than only at the gateway.
#: Same set as `gateway/app/auth.py::MONEY_ROLES` -- duplicated because this repo
#: has no shared library, and a rule that holds in one service and not the other
#: is not a rule. `test_money_role_matches_the_gateway` holds the two together.
MONEY_ROLES = frozenset({"csr", "admin"})


@dataclass(frozen=True)
class Principal:
    """A verified human. Every field came out of a checked signature."""

    subject: str
    role: str
    issued_at: int
    expires_at: int
    assertion_id: str

    def may_move_money(self) -> bool:
        return self.role in MONEY_ROLES


class PrincipalVerificationError(Exception):
    """The assertion could not be verified. Never carries the reason to a caller."""


def _public_key():
    if not config.PRINCIPAL_VERIFY_KEY:
        raise PrincipalVerificationError("PRINCIPAL_VERIFY_KEY is not configured")
    try:
        key = serialization.load_pem_public_key(
            config.PRINCIPAL_VERIFY_KEY.encode("utf-8")
        )
    except Exception as exc:  # noqa: BLE001
        raise PrincipalVerificationError(
            f"PRINCIPAL_VERIFY_KEY is not a readable PEM public key: {type(exc).__name__}"
        ) from exc
    if key.__class__.__name__ != "Ed25519PublicKey":
        raise PrincipalVerificationError(
            f"PRINCIPAL_VERIFY_KEY is a {key.__class__.__name__}, not Ed25519"
        )
    return key


def verify(assertion: str | None) -> Principal:
    """Turn a header value into a verified `Principal`, or refuse.

    Raises `PrincipalVerificationError` for every failure mode. The caller turns
    that into a 401 with a fixed message: telling an unauthenticated caller
    *which* check failed hands them a tuning signal for the next attempt.
    """
    if not assertion:
        raise PrincipalVerificationError("no assertion presented")

    try:
        claims = jwt.decode(
            assertion,
            _public_key(),
            algorithms=ALGORITHMS,
            audience=config.ASSERTION_AUDIENCE,
            issuer=config.ASSERTION_ISSUER,
            leeway=LEEWAY_SECONDS,
            options={
                # Named rather than defaulted. PyJWT verifies exp/aud/iss when
                # the arguments are supplied, but `nbf` and the *presence* of
                # each claim are opt-in -- and "absent" must not read as
                # "satisfied", which is how a token with no expiry passes an
                # expiry check.
                "require": ["exp", "iat", "nbf", "iss", "aud", "sub"],
                "verify_exp": True,
                "verify_nbf": True,
                "verify_iat": True,
                "verify_aud": True,
                "verify_iss": True,
                "verify_signature": True,
            },
        )
    except jwt.InvalidTokenError as exc:
        raise PrincipalVerificationError(
            f"assertion rejected: {type(exc).__name__}"
        ) from exc

    subject = str(claims.get("sub") or "")
    role = str(claims.get("role") or "")
    if not subject:
        raise PrincipalVerificationError("assertion carries no subject")
    if not role:
        raise PrincipalVerificationError("assertion carries no role")

    # An assertion may not outlive the TTL the issuer is configured to grant.
    # Without this, a gateway misconfigured to a one-week TTL -- or a key that
    # leaked and is being used to mint long-lived tokens -- would be accepted by
    # a verifier that only checks that `exp` is in the future.
    lifetime = int(claims["exp"]) - int(claims["iat"])
    if lifetime > config.ASSERTION_MAX_LIFETIME_SECONDS:
        raise PrincipalVerificationError(
            f"assertion lifetime {lifetime}s exceeds the permitted maximum "
            f"{config.ASSERTION_MAX_LIFETIME_SECONDS}s"
        )

    return Principal(
        subject=subject,
        role=role,
        issued_at=int(claims["iat"]),
        expires_at=int(claims["exp"]),
        assertion_id=str(claims.get("jti", "")),
    )


def require_money_principal(assertion: str | None,
                            claimed_role: str | None = None,
                            claimed_user: str | None = None) -> Principal:
    """The guard money-moving staff routes call. Fails closed, always.

    `claimed_role` / `claimed_user` are the caller-supplied `X-User-*` headers.
    They are NOT consulted for authority. They are passed in only so that a
    disagreement with the verified assertion can be refused outright rather than
    quietly ignored (REQ-ID-8): a request whose headers say `admin` while its
    signature says `csr` is not a confused client, and serving it -- even
    correctly, at the lower authority -- would leave the attempt invisible.
    """
    try:
        principal = verify(assertion)
    except PrincipalVerificationError as exc:
        # Logged with the reason, answered without it.
        log.warning("principal rejected: %s", exc)
        raise HTTPException(status_code=401, detail="not authorized") from exc

    if claimed_role is not None and claimed_role != principal.role:
        log.warning(
            "identity mismatch: header role %r against verified role %r for subject %s",
            claimed_role, principal.role, principal.subject,
        )
        raise HTTPException(status_code=401, detail="not authorized")
    if claimed_user is not None and claimed_user != principal.subject:
        log.warning(
            "identity mismatch: header subject %r against verified subject %r",
            claimed_user, principal.subject,
        )
        raise HTTPException(status_code=401, detail="not authorized")

    if not principal.may_move_money():
        # 403, not 401: this caller IS authenticated and is not permitted. The
        # distinction matters to an operator reading logs -- one is a broken
        # deployment, the other is a staff member hitting a boundary.
        log.warning(
            "role %r may not move money (subject %s)", principal.role, principal.subject
        )
        raise HTTPException(status_code=403, detail="csr/admin only")

    return principal


def seconds_until_expiry(principal: Principal) -> int:
    """Diagnostic helper for logs and tests; not an authorization decision."""
    return principal.expires_at - int(time.time())
