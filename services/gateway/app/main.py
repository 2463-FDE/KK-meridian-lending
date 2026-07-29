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
import os
import re

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
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

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
log = logging.getLogger("gateway")

app = FastAPI(title="Meridian Gateway (BFF)", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],          # wide-open CORS (brownfield)
    allow_methods=["*"],
    allow_headers=["*"],
)


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
    try:
        return JSONResponse(status_code=resp.status_code, content=resp.json())
    except Exception:
        return JSONResponse(status_code=resp.status_code, content={"raw": resp.text})


def _require_user(authorization: str | None) -> dict:
    user = auth.get_session(auth.bearer_token(authorization))
    if not user:
        raise HTTPException(status_code=401, detail="not authenticated")
    return user


@app.api_route("/los/{path:path}", methods=["GET", "POST"])
async def los(path: str, request: Request, authorization: str | None = Header(None)):
    # Origination is borrower-facing; an applicant can apply without an account.
    # If a session is present we forward it, otherwise we proxy anonymously.
    user = auth.get_session(auth.bearer_token(authorization))
    return await _proxy(ORIGINATION_URL, f"/{path}", request, user)


def _borrower_loans(applicant_id: int) -> dict:
    """A borrower's own loan list -- built directly from the shared DB rather
    than proxied through servicing-service's /loans (which has no ownership
    filter of its own). Same shape as servicing-service's Page[LoanListItem]
    so the frontend needs no special-casing for the borrower path."""
    rows = db.query(
        "SELECT l.id, l.applicant_name, l.principal, l.apr, l.term_months, l.status, "
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
            "apr": float(r["apr"]),
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

    if path == "loans" and request.method == "GET":
        if auth.is_staff(user):
            return await _proxy(SERVICING_URL, f"/{path}", request, user)
        if user.get("applicant_id"):
            return JSONResponse(content=_borrower_loans(user["applicant_id"]))
        raise HTTPException(status_code=403, detail="forbidden")

    loan_match = _LOAN_SUBPATH_RE.match(path)
    if loan_match and request.method == "GET":
        loan_id = loan_match.group(1)
        if auth.is_staff(user) or auth.owns_loan(user, loan_id):
            return await _proxy(SERVICING_URL, f"/{path}", request, user)
        raise HTTPException(status_code=403, detail="forbidden")

    account_match = _ACCOUNT_ACTION_RE.match(path)
    if account_match:
        loan_id, action = account_match.group(1), account_match.group(2)
        if action in _ACCOUNT_READ_ACTIONS:
            if auth.is_staff(user) or auth.owns_loan(user, loan_id):
                return await _proxy(SERVICING_URL, f"/{path}", request, user)
            raise HTTPException(status_code=403, detail="forbidden")
        # adjust-balance / waive-fee / late-fee -- CSR/admin only. Underwriter is
        # staff but has no business moving money; is_staff() alone let an
        # underwriter POST straight to these routes even though the servicing UI
        # never shows them the button.
        if auth.can_move_money(user):
            return await _proxy(SERVICING_URL, f"/{path}", request, user)
        raise HTTPException(status_code=403, detail="csr/admin only")

    if path == "reconciliation/peek":
        if auth.is_staff(user):
            return await _proxy(SERVICING_URL, f"/{path}", request, user)
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
    # caller could POST /decision/decisions directly with an SSN, triggering a
    # real credit pull and overwriting the decision for any existing application
    # via the upsert. The normal decision flow never comes through this route at
    # all -- origination-service calls decision-service server-to-server over the
    # internal network -- so this proxy only exists for staff/ops tooling to
    # inspect or re-run a decision directly. Staff-only, no exceptions.
    user = _require_user(authorization)
    if not auth.is_staff(user):
        raise HTTPException(status_code=403, detail="staff only")
    return await _proxy(
        DECISION_URL, f"/{path}", request, user,
        extra_headers={"X-Internal-Token": INTERNAL_SERVICE_TOKEN},
    )


@app.api_route("/disclosure/{path:path}", methods=["GET", "POST"])
async def disclosure(path: str, request: Request, authorization: str | None = Header(None)):
    user = auth.get_session(auth.bearer_token(authorization))
    return await _proxy(DISCLOSURE_URL, f"/{path}", request, user)


@app.api_route("/payments/{path:path}", methods=["GET", "POST"])
async def payments(path: str, request: Request, authorization: str | None = Header(None)):
    # Pre-existing bug fixed in passing: unlike /los or /lss, payment-service's
    # own route is literally POST /payments (not POST /) -- proxying to
    # f"/{path}" (empty here) hit payment-service's bare "/" and 404'd for
    # EVERY caller, staff included. Hardcoded below since this is the one real
    # endpoint this proxy has ever forwarded to.
    user = _require_user(authorization)

    if path == "" and request.method == "POST":
        if auth.is_staff(user):
            return await _proxy(PAYMENT_URL, "/payments", request, user)
        # Borrower: only allowed to charge a loan their own applicant_id owns.
        # request.body() is cached by Starlette after the first read, so _proxy's
        # own await request.body() below still gets the same bytes.
        try:
            payload = json.loads(await request.body())
            loan_id = payload.get("loan_id")
        except Exception:
            loan_id = None
        if loan_id is not None and auth.owns_loan(user, loan_id):
            return await _proxy(PAYMENT_URL, "/payments", request, user)
        raise HTTPException(status_code=403, detail="forbidden")

    raise HTTPException(status_code=404, detail="not found")


@app.api_route("/assistant/{path:path}", methods=["GET", "POST"])
async def assistant(path: str, request: Request, authorization: str | None = Header(None)):
    # AI summary returns risk tier + internal flags — staff only, not the borrower.
    user = _require_user(authorization)
    if not auth.is_staff(user):
        raise HTTPException(status_code=403, detail="Forbidden")
    return await _proxy(LOAN_ASSISTANT_URL, f"/{path}", request, user)
