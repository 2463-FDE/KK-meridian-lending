"""Fixed-window rate limiting, per client IP, backed by Redis.

The gateway is the single front door for every request into the platform --
including anonymous /los/* traffic from an unauthenticated applicant -- and had
no rate limiting of any kind before this: a single caller could hammer any
endpoint, staff-authenticated or not, at no cost. A fixed-window counter
(INCR + EXPIRE) is the right amount of protection for a brownfield platform's
first rate limit, not a full token-bucket/leaky-bucket implementation.

Fails open on a Redis error: a rate-limiter outage must not become a second way
to take the whole gateway down (same reasoning as every other "a hiccup here
must not fail the thing that already works" guard in this codebase).
"""
import logging
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from . import auth
from .config import RATE_LIMIT_MAX_REQUESTS, RATE_LIMIT_WINDOW_SECONDS

log = logging.getLogger("gateway.rate_limit")

_EXEMPT_PATHS = {"/health"}


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in _EXEMPT_PATHS:
            return await call_next(request)

        client_ip = request.client.host if request.client else "unknown"
        window = int(time.time()) // RATE_LIMIT_WINDOW_SECONDS
        key = f"ratelimit:{client_ip}:{window}"

        try:
            r = auth._client()
            count = r.incr(key)
            if count == 1:
                r.expire(key, RATE_LIMIT_WINDOW_SECONDS)
        except Exception as e:  # noqa -- Redis hiccup must not block all traffic
            log.warning("rate limiter unavailable, failing open: %s", e)
            return await call_next(request)

        if count > RATE_LIMIT_MAX_REQUESTS:
            return JSONResponse(
                status_code=429,
                content={"detail": "rate limit exceeded, try again shortly"},
            )

        return await call_next(request)
