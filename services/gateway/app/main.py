"""API gateway / BFF — FastAPI.

Fronts the Next.js portal and routes to the LOS and LSS services. Adds a session-auth
layer: `/auth/*` for login/logout, and a guard on the servicing (`/lss/*`) and
(`/payments/*`) routes. The resolved identity is forwarded downstream as
`X-User-Id` / `X-User-Role` headers.

Review finding: the gateway used to authenticate the caller but NOT enforce role/
ownership on servicing or payment routes -- any authenticated user, including a
borrower, could read or act on ANY loan (list the whole portfolio, read another
borrower's balance/payment history, adjust a balance, waive a fee). /lss/* and
/payments/* now split into three tiers: staff-only (portfolio list, balance
adjustments, fee waivers, reconciliation), owner-or-staff (a specific loan's
detail/schedule/payment-history/balance, and charging a payment -- staff for any
loan, a borrower only for a loan their own applicant_id owns), and fail-closed
(anything else under these prefixes gets a 404 rather than being silently proxied
with no authz decision made for it).
"""
import json
import logging
import math
import os
import re

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel

from . import auth, db
from .config import (
    DECISION_URL,
    DISCLOSURE_URL,
    INTERNAL_SERVICE_TOKEN,
    KYC_URL,
    LOAN_ASSISTANT_URL,
    ORIGINATION_URL,
    PAYMENT_URL,
    SERVICING_URL,
)
from .rate_limit import RateLimitMiddleware

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("gateway")

app = FastAPI(title="Meridian Gateway (BFF)", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # wide-open CORS (brownfield)
    allow_methods=["*"],
    allow_headers=["*"],
)
# Middleware runs in reverse-add order (last added runs first) -- rate limiting
# added after CORS so it's the first check a request actually hits.
app.add_middleware(RateLimitMiddleware)

# W7: exposes GET /metrics in Prometheus text format -- request count, latency
# histograms, in-progress requests, broken down by route/method/status. No
# service in this repo had any cross-service metrics before this; LangSmith
# only ever covered the LLM calls, not the other seven services.
Instrumentator().instrument(app).expose(app)

# W7: exposes GET /metrics in Prometheus text format -- request count, latency
# histograms, in-progress requests, broken down by route/method/status. No
# service in this repo had any cross-service metrics before this; LangSmith
# only ever covered the LLM calls, not the other seven services.
Instrumentator().instrument(app).expose(app)


@app.get("/health")
def health():
    return {"status": "ok", "service": "gateway"}


# --------------------------------------------------------------------------- auth

class LoginIn(BaseModel):
    username: str
    password: str


@app.post("/auth/login")
def login(body: LoginIn):
    try:
        user = auth.authenticate(body.username, body.password)
    except Exception as e:  # DB/redis down
        log.warning("login backend error: %s", e)
        raise HTTPException(status_code=503, detail="auth backend unavailable")
    if not user:
        raise HTTPException(status_code=401, detail="invalid username or password")
    token = auth.create_session(user)
    return {"token": token, "user": user}


@app.post("/auth/logout")
def logout(authorization: str | None = Header(None)):
    auth.delete_session(auth.bearer_token(authorization))
    return {"ok": True}


@app.get("/auth/me")
def me(authorization: str | None = Header(None)):
    user = auth.get_session(auth.bearer_token(authorization))
    if not user:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


# -------------------------------------------------------------------------- proxy

async def _proxy(base: str, path: str, request: Request, user: dict | None, extra_headers: dict | None = None):
    method = request.method
    body = await request.body()
    # Security fix (borrower-workflow audit): the borrower's one-time
    # accept_token used to travel as a URL query parameter on the offer
    # GET route -- that leaks into this gateway's own access log AND its
    # outbound httpx request log below, plus browser history/Referer. It
    # now travels only as X-Offer-Accept-Token, a plain header -- forwarded
    # here intentionally, same as every other inbound header that isn't an
    # identity claim (X-User-*, stripped below) or connection-level
    # (host/content-length/authorization, replaced by the resolved
    # session). This proxy never re-serializes any header into the
    # outbound URL -- headers stay headers (see the httpx call below,
    # `headers=headers` is separate from `params=request.query_params`).
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "authorization")
        and not k.lower().startswith("x-user-")
    }
    if user:
        headers["X-User-Id"] = str(user.get("id", ""))
        headers["X-User-Role"] = str(user.get("role", ""))
    if extra_headers:
        headers.update(extra_headers)
    async with httpx.AsyncClient(timeout=35) as client:
        resp = await client.request(
            method, f"{base}{path}", content=body, headers=headers,
            params=request.query_params,
        )
    # Bug fix: resp.json() decodes via resp.text, which falls back to
    # httpx's charset auto-detection whenever the upstream response's
    # Content-Type has no explicit charset param (every backend service here
    # just sends "application/json" with none). Auto-detection can misguess
    # short multi-byte sequences as Latin-1/cp1252 -- an en dash or an
    # accented name came back through this proxy as visible mojibake
    # ("Jos\xc3\xa9" instead of "Jos\xe9") on every route, not just one.
    # RFC 8259 mandates JSON is UTF-8 (unless a BOM says otherwise) -- decode
    # the raw bytes as UTF-8 directly instead of letting httpx guess.
    try:
        return JSONResponse(status_code=resp.status_code, content=json.loads(resp.content.decode("utf-8")))
    except Exception:
        return JSONResponse(status_code=resp.status_code, content={"raw": resp.content.decode("utf-8", errors="replace")})


def _require_user(authorization: str | None) -> dict:
    user = auth.get_session(auth.bearer_token(authorization))
    if not user:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


@app.api_route("/los/{path:path}", methods=["GET", "POST"])
async def los(path: str, request: Request, authorization: str | None = Header(None)):
    # Origination is borrower-facing; an applicant can apply without an account.
    # If a session is present we forward it, otherwise we proxy anonymously.
    #
    # Review fix: origination-service's own staff-only routes (financials,
    # rerun-decision, history, accept/re-accept) trust X-User-Role alone --
    # docker-compose.yml no longer publishes its host port, but that's network
    # topology, not an application-level check. X-Internal-Token (the same
    # shared secret already forwarded to decision-service/disclosure-service/
    # payment-service above) proves this request actually came through the
    # gateway; a caller who reaches origination-service directly (e.g. if the
    # port is ever mistakenly reopened) can still fake X-User-Role but doesn't
    # know this secret, so it fails origination-service's own staff check too.
    user = auth.get_session(auth.bearer_token(authorization))
    return await _proxy(
        ORIGINATION_URL, f"/{path}", request, user,
        extra_headers={"X-Internal-Token": INTERNAL_SERVICE_TOKEN},
    )


def _borrower_loans(applicant_id: int) -> dict:
    """A borrower's own loan list -- built directly from the shared DB rather
    than proxied through servicing-service's /loans (which has no ownership
    filter of its own). Same shape as servicing-service's Page[LoanListItem]
    so the frontend needs no special-casing for the borrower path."""
    rows = db.query(
        "SELECT l.id, l.applicant_name, l.principal, l.apr, l.schedule_version, "
        "       l.term_months, l.status, "
        "       COALESCE(b.balance, 0) AS balance, COALESCE(b.past_due, 0) AS past_due, "
        "       l.opened_at "
        "FROM loans l "
        "JOIN applications a ON a.id = l.app_id "
        "LEFT JOIN balances b ON b.loan_id = l.id "
        "WHERE a.applicant_id = %s "
        "ORDER BY l.id",
        (applicant_id,),
    )
    items = [
        {
            "id": r["id"],
            "applicant_name": r["applicant_name"],
            # NUMERIC columns come back as Decimal from raw psycopg2 (unlike a
            # SQLAlchemy read, this isn't affected by any asdecimal setting) --
            # JSONResponse below uses stdlib json.dumps, which can't serialize
            # Decimal. Cast to float at this boundary, same fix as everywhere
            # else a raw-DB-read money value crosses a JSON response/request.
            "principal": float(r["principal"]),
            # `loans.apr` means different things depending on how the loan was
            # boarded: the contractual note rate under the current path, the
            # DISCLOSED APR under the pre-change one (5.196% for a contract
            # priced at 7.99%). `schedule_version` is set only by the current
            # path, so it is the evidence the value means what the API calls
            # it -- the same rule servicing-service applies. Reported only where
            # it is proven; unknown stays unknown rather than printing a rate
            # the borrower was never quoted. Reviewed on PR #10.
            "note_rate_pct": (float(r["apr"]) if r.get("schedule_version") else None),
            "note_rate_proven": bool(r.get("schedule_version")),
            "term_months": r["term_months"],
            "status": r["status"],
            "balance": float(r["balance"]),
            "past_due": float(r["past_due"]),
            "opened_at": r["opened_at"].isoformat() if r["opened_at"] else None,
        }
        for r in rows
    ]
    return {"items": items, "total": len(items), "limit": len(items) or 1, "offset": 0}


_LOAN_SUBPATH_RE = re.compile(r"^loans/(\d+)(?:/(schedule|payments))?$")
_ACCOUNT_ACTION_RE = re.compile(r"^accounts/(\d+)/(balance|adjust-balance|waive-fee|late-fee)$")
# Read-only, ownership-checked for a borrower; every other accounts/ action below
# (adjust-balance, waive-fee, late-fee) is a money-moving action -- CSR/admin only
# (underwriter is staff but not permitted to move money -- see can_move_money).
_ACCOUNT_READ_ACTIONS = ("balance",)


@app.api_route("/lss/{path:path}", methods=["GET", "POST"])
async def lss(path: str, request: Request, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    # servicing-service now requires X-Internal-Token on its money-moving routes
    # (see its main.py::_require_internal), so every proxy call below forwards it
    # or a real staff session gets 401'd. Bound once rather than repeated at each
    # `_proxy(...)` -- there are five of them in this function alone.
    svc = {"X-Internal-Token": INTERNAL_SERVICE_TOKEN}

    if path == "loans" and request.method == "GET":
        if auth.is_staff(user):
            return await _proxy(SERVICING_URL, f"/{path}", request, user, extra_headers=svc)
        if user.get("applicant_id"):
            return JSONResponse(content=_borrower_loans(user["applicant_id"]))
        raise HTTPException(status_code=403, detail="forbidden")

    loan_match = _LOAN_SUBPATH_RE.match(path)
    if loan_match and request.method == "GET":
        loan_id = loan_match.group(1)
        if auth.is_staff(user) or auth.owns_loan(user, loan_id):
            return await _proxy(SERVICING_URL, f"/{path}", request, user, extra_headers=svc)
        raise HTTPException(status_code=403, detail="forbidden")

    account_match = _ACCOUNT_ACTION_RE.match(path)
    if account_match:
        loan_id, action = account_match.group(1), account_match.group(2)
        if action in _ACCOUNT_READ_ACTIONS:
            if auth.is_staff(user) or auth.owns_loan(user, loan_id):
                return await _proxy(SERVICING_URL, f"/{path}", request, user, extra_headers=svc)
            raise HTTPException(status_code=403, detail="forbidden")
        # adjust-balance / waive-fee / late-fee -- CSR/admin only. Underwriter is
        # staff but has no business moving money; is_staff() alone let an
        # underwriter POST straight to these routes even though the servicing UI
        # never shows them the button.
        if auth.can_move_money(user):
            return await _proxy(SERVICING_URL, f"/{path}", request, user, extra_headers=svc)
        raise HTTPException(status_code=403, detail="csr/admin only")

    if path == "reconciliation/peek":
        if auth.is_staff(user):
            return await _proxy(SERVICING_URL, f"/{path}", request, user, extra_headers=svc)
        raise HTTPException(status_code=403, detail="staff only")

    # Unrecognized /lss sub-path (including the legacy servicing-service /payments
    # duplicate, and apply-payment which payment-service calls servicing-service
    # for directly and should never be reachable through this proxy at all) --
    # fail closed rather than proxy something no authz rule above accounted for.
    raise HTTPException(status_code=404, detail="not found")


# --- LOS sub-services (the decomposed origination estate). -------------------
# Origination calls these server-to-server during the application flow; they are
# also exposed here so the portal / ops tooling can reach each service directly.
# Like /los/*, the underwriting-flow services forward a session if one is present
# but do not require it (an applicant can apply without an account).

@app.api_route("/kyc/{path:path}", methods=["GET", "POST"])
async def kyc(path: str, request: Request, authorization: str | None = Header(None)):
    user = auth.get_session(auth.bearer_token(authorization))
    return await _proxy(KYC_URL, f"/{path}", request, user)


@app.api_route("/decision/{path:path}", methods=["GET", "POST"])
async def decision(path: str, request: Request, authorization: str | None = Header(None)):
    # Security fix: this used to forward with an optional session -- an anonymous
    # caller could POST /decision/decisions directly (bypassing origination-service
    # entirely) with a guessed application_id and fabricated income/SSN, and
    # decision-service's ON CONFLICT DO UPDATE would overwrite the real underwriting
    # outcome + its audit trail. The normal decision flow never comes through this
    # route at all -- origination-service calls decision-service server-to-server
    # over the internal network -- so this proxy only exists for staff/ops tooling
    # to inspect or re-run a decision directly. Staff-only, no exceptions.
    # decision-service itself now requires X-Internal-Token on every call (see
    # routers/decisions.py) -- this proxy has to forward it too, or a staff
    # session hitting decision-service through the gateway gets 401'd.
    user = _require_user(authorization)
    if not auth.is_staff(user):
        raise HTTPException(status_code=403, detail="staff only")
    return await _proxy(
        DECISION_URL, f"/{path}", request, user,
        extra_headers={"X-Internal-Token": INTERNAL_SERVICE_TOKEN},
    )


@app.api_route("/disclosure/{path:path}", methods=["GET", "POST"])
async def disclosure(path: str, request: Request, authorization: str | None = Header(None)):
    # Security fix: same gap as /decision/* above -- an anonymous caller could POST
    # /disclosure/offers directly with an approved application_id and any
    # principal/rate/term, overwriting the canonical TILA disclosure. The normal
    # auto-offer flow never comes through this route -- origination-service's
    # disclosure_graph calls disclosure-service server-to-server. Staff-only.
    # (disclosure-service's own create_offer also now derives principal/term
    # server-side rather than trusting the body -- see offers.py -- so this is
    # defense in depth, not the only fix.)
    user = _require_user(authorization)
    if not auth.is_staff(user):
        raise HTTPException(status_code=403, detail="staff only")
    return await _proxy(
        DISCLOSURE_URL, f"/{path}", request, user,
        extra_headers={"X-Internal-Token": INTERNAL_SERVICE_TOKEN},
    )


# Review fix: payment-service is the authoritative amount check (schemas.PaymentIn
# rejects this same range), but the gateway is the first hop every caller (staff
# and borrower alike) passes through -- reject here too instead of trusting the
# proxy target to catch it, same ceiling as payment-service's _MAX_AMOUNT.
_MAX_PAYMENT_AMOUNT = 1_000_000.00


def _valid_amount(amount) -> bool:
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        return False
    return math.isfinite(amount) and 0 < amount <= _MAX_PAYMENT_AMOUNT


@app.api_route("/payments/{path:path}", methods=["GET", "POST"])
async def payments(path: str, request: Request, authorization: str | None = Header(None)):
    # Pre-existing bug fixed in passing: unlike /los or /lss, payment-service's
    # own route is literally POST /payments (not POST /) -- proxying to
    # f"/{path}" (empty here) hit payment-service's bare "/" and 404'd for
    # EVERY caller, staff included. Hardcoded below since this is the one real
    # endpoint this proxy has ever forwarded to.
    user = _require_user(authorization)

    if path == "" and request.method == "POST":
        payment_headers = {"X-Internal-Token": INTERNAL_SERVICE_TOKEN}
        # request.body() is cached by Starlette after the first read, so _proxy's
        # own await request.body() below still gets the same bytes.
        try:
            payload = json.loads(await request.body())
        except Exception:
            payload = {}

        # Review fix: amount was forwarded unchecked -- a negative value credited
        # the borrower's balance instead of charging them (servicing computes
        # new_balance = current - amount), and zero/NaN/Infinity all passed
        # through too. Enforced for staff and borrower alike.
        if not _valid_amount(payload.get("amount")):
            raise HTTPException(status_code=400, detail="amount must be a positive number")

        if auth.is_staff(user):
            return await _proxy(PAYMENT_URL, "/payments", request, user, extra_headers=payment_headers)
        # Borrower: only allowed to charge a loan their own applicant_id owns.
        loan_id = payload.get("loan_id")
        if loan_id is not None and auth.owns_loan(user, loan_id):
            return await _proxy(PAYMENT_URL, "/payments", request, user, extra_headers=payment_headers)
        raise HTTPException(status_code=403, detail="forbidden")

    raise HTTPException(status_code=404, detail="not found")


@app.post("/assistant/policy-chat")
async def assistant_policy_chat(request: Request, authorization: str | None = Header(None)):
    # Registered before the /assistant/{path:path} catch-all below so this literal
    # path wins the match. Policy Q&A is generic lending-policy content -- no
    # per-applicant financials or risk_tier -- so it doesn't need the staff-only
    # gate that protects /assistant/applications/*/summary; a borrower can ask
    # without an account, same anonymous-allowed pattern as /los/*.
    # loan-assistant's own cost guard (MAX_INPUT_TOKENS) and this gateway's
    # per-IP rate limiter both already apply regardless of caller identity.
    user = auth.get_session(auth.bearer_token(authorization))
    return await _proxy(LOAN_ASSISTANT_URL, "/policy-chat", request, user)


@app.api_route("/assistant/{path:path}", methods=["GET", "POST"])
async def assistant(path: str, request: Request, authorization: str | None = Header(None)):
    # AI summary returns risk tier + internal flags — staff only, not the
    # borrower. (Policy Q&A is split out above -- no per-applicant financials there.)
    user = _require_user(authorization)
    if not auth.is_staff(user):
        raise HTTPException(status_code=403, detail="Forbidden")
    return await _proxy(LOAN_ASSISTANT_URL, f"/{path}", request, user)
