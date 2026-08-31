"""One definition of "is this caller staff, and with what authority".

`routers/applications.py` grew this check inline and every staff-gated route
there calls it. RF-25 adds routes with a NARROWER role set -- underwriter and
admin only, never CSR -- and a second inline copy of the shared-secret
comparison is exactly how two answers to one question start disagreeing. The
role set differs per route; the token check does not, so the token check lives
here once and each route names the roles it accepts.
"""
import secrets

from fastapi import HTTPException

from . import config

#: Roles allowed to see underwriting-sensitive fields. The historical set.
STAFF_ROLES = frozenset({"csr", "underwriter", "admin"})

#: RF-25: the client authorised manual DTI for these two roles only. A CSR is
#: staff and is deliberately not here -- see routers/manual_dti.py.
UNDERWRITING_ROLES = frozenset({"underwriter", "admin"})


def internal_token_is_valid(x_internal_token: str | None) -> bool:
    """Constant-time comparison against the configured shared secret.

    The gateway attaches this on every proxied call. A caller reaching this
    service directly can claim any `X-User-Role` it likes but does not know the
    secret, so the role claim alone is never enough.
    """
    if not config.INTERNAL_SERVICE_TOKEN or not x_internal_token:
        return False
    return secrets.compare_digest(x_internal_token.encode("utf-8"),
                                  config.INTERNAL_SERVICE_TOKEN.encode("utf-8"))


def has_role(x_user_role: str | None, x_internal_token: str | None,
             allowed: frozenset[str]) -> bool:
    return x_user_role in allowed and internal_token_is_valid(x_internal_token)


def require_role(x_user_role: str | None, x_internal_token: str | None,
                 allowed: frozenset[str], detail: str) -> None:
    """403 with one message for every failure mode.

    A rejected caller learns that it was rejected and nothing else: whether the
    token was wrong, the role was wrong, or both, the response is identical. A
    message that distinguished them would tell an unauthenticated caller which
    half of the check to work on.
    """
    if not has_role(x_user_role, x_internal_token, allowed):
        raise HTTPException(status_code=403, detail=detail)
