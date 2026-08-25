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

from . import agent_trace

from . import auth, config, db, principal
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

# Fail at boot rather than per-request if the internal token is unusable
# (PR #18 review). Import-time so an unusable deployment never serves traffic.
config.validate_internal_token()
# The key that lets this gateway say WHO is acting. Validated at boot for the
# same reason as the token: a malformed key fails at mint time, which is a staff
# money request in production.
config.validate_principal_signing_key()

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
    # `x-internal-token` is stripped for the same reason as `x-user-*`: it is an
    # identity claim this gateway makes, never one a client is allowed to assert.
    # Leaving it through was not merely untidy -- it was caller-controlled. Header
    # names arrive lowercased, so a client's `X-Internal-Token: junk` survived as
    # the key `x-internal-token`, and the `headers.update()` below then added
    # `X-Internal-Token` as a SECOND, differently-cased key rather than replacing
    # it. Both went on the wire, and the downstream `Header(alias=...)` resolves
    # through Starlette's `Headers.get`, which returns the first match -- the
    # client's. So any caller could hand the gateway a junk token and force a 401
    # on every internal-token route: /kyc/*, /decision/*, /disclosure/*,
    # /payments/*, and origination's staff checks on /los/*. Fails closed, so it
    # was availability rather than escalation, but it was a stranger's switch.
    #
    # The trace-propagation headers are stripped here too, on EVERY route. They
    # are how a LangSmith parent context travels between services
    # (`RunTree.to_headers()`), and this proxy forwards inbound headers by
    # default -- so without this line a caller could hand us its own
    # `langsmith-trace` and have Meridian's internal spans attach under a tree
    # it chose. The authoritative context is minted by this service, after
    # authorisation, and put back via `extra_headers` below; anything the caller
    # sent under those names does not survive this hop.
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in ("host", "content-length", "authorization",
                             "x-internal-token", "x-principal-assertion")
        and k.lower() not in agent_trace.PROPAGATION_HEADERS
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
        "SELECT l.id, l.applicant_name, l.principal, l.note_rate_pct, "
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
            # D19 contract (db/migrations/0039): one column, no inference.
            #
            # `loans.apr` MEANT different things depending on how the loan was
            # boarded: the contractual note rate under the current path, the
            # DISCLOSED APR under the pre-change one (5.196% for a contract
            # priced at 7.99%). So this read preferred `note_rate_pct` and fell
            # back to `apr` only where `schedule_version` proved which figure
            # was stored, reporting nothing otherwise rather than printing a
            # rate the borrower was never quoted (reviewed on PR #10).
            #
            # `apr` is gone and `note_rate_pct` is NOT NULL, so there is nothing
            # to fall back to and nothing that can be unproven -- the same
            # simplification servicing's `_proven_note_rate` just made. The
            # `note_rate_proven` field stays in the response because clients
            # branch on it.
            "note_rate_pct": float(r["note_rate_pct"]),
            "note_rate_proven": True,
            "term_months": r["term_months"],
            "status": r["status"],
            "balance": float(r["balance"]),
            "past_due": float(r["past_due"]),
            "opened_at": r["opened_at"].isoformat() if r["opened_at"] else None,
        }
        for r in rows
    ]
    return {"items": items, "total": len(items), "limit": len(items) or 1, "offset": 0}


#: The owner-or-staff loan reads. `activity` joined `schedule` and `payments`
#: here rather than getting a rule of its own: it is the same authority
#: question -- your loan or a loan you service -- and a second rule is a
#: second place for the ownership check to be forgotten. The alternation
#: stays CLOSED: an unlisted sub-path falls through to the 404 below, which
#: is what keeps `/lss/*` from being a generic proxy.
_LOAN_SUBPATH_RE = re.compile(r"^loans/(\d+)(?:/(schedule|payments|activity))?$")
_MOVEMENT_RESOLVE_RE = re.compile(r"^movements/(\d+)/resolve$")
#: The disposition route on one review-queue item. Anchored and numeric for the
#: same reason as the one above: a permissive pattern here decides which paths
#: reach servicing at all, and the fall-through below is a 404 by design.
_REVIEW_DISPOSITION_RE = re.compile(r"^reconciliation/review-queue/(\d+)/disposition$")
_ACCOUNT_ACTION_RE = re.compile(r"^accounts/(\d+)/(balance|adjust-balance|waive-fee|late-fee)$")
# Read-only, ownership-checked for a borrower; every other accounts/ action below
# (adjust-balance, waive-fee, late-fee) is a money-moving action -- CSR/admin only
# (underwriter is staff but not permitted to move money -- see can_move_money).
_ACCOUNT_READ_ACTIONS = ("balance",)

#: Actions that raise a maker-checker proposal rather than moving money. Any
#: staff role may reach these -- the authority to APPROVE is a separate question
#: that servicing answers against the configured threshold.
_PROPOSAL_ACTIONS = ("adjust-balance", "waive-fee")


def _principal_headers(svc: dict, user: dict) -> dict:
    """Service headers plus a freshly minted human principal, or 503.

    Every LSS path that mints goes through here. Three call sites each doing
    their own try/except is three chances to forget one -- and forgetting it
    turns a missing signing key into a generic 500, which points an operator at
    the wrong service entirely. A money route that cannot say who is acting must
    refuse, and must say why.
    """
    headers = dict(svc)
    try:
        headers[principal.HEADER] = principal.mint(user)
    except config.PrincipalKeyError as exc:
        log.error("cannot mint a principal assertion: %s", exc)
        raise HTTPException(
            status_code=503, detail="identity signing unavailable",
        ) from exc
    return headers


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
        # `late-fee` still moves money on one person's say-so, so it stays
        # csr/admin: an underwriter is staff and has no business moving a balance
        # alone. `adjust-balance` and `waive-fee` are different since the
        # maker-checker cutover -- they raise PROPOSALS and move nothing, and an
        # underwriter is the role that does most of the approving. Refusing them
        # here would block the control's main reviewer from using it.
        if action in _PROPOSAL_ACTIONS:
            if auth.is_staff(user):
                return await _proxy(SERVICING_URL, f"/{path}", request, user,
                                    extra_headers=_principal_headers(svc, user))
            raise HTTPException(status_code=403, detail="staff only")

        # adjust-balance / waive-fee / late-fee -- CSR/admin only. Underwriter is
        # staff but has no business moving money; is_staff() alone let an
        # underwriter POST straight to these routes even though the servicing UI
        # never shows them the button.
        #
        # The check below is no longer the only one. Servicing verifies the
        # signed assertion minted here and applies the same csr/admin rule
        # itself, so this hop is defence in depth rather than the boundary: a
        # caller that skips the gateway entirely is now refused by servicing
        # instead of arriving unauthenticated-as-a-human with a shared token.
        if auth.can_move_money(user):
            return await _proxy(SERVICING_URL, f"/{path}", request, user,
                                extra_headers=_principal_headers(svc, user))
        raise HTTPException(status_code=403, detail="csr/admin only")

    # The maker-checker queue and the resolve endpoint. Staff-only here; WHICH
    # staff may approve what is servicing's decision, made against the verified
    # principal and the configured threshold rather than at this hop.
    if path == "movements" or _MOVEMENT_RESOLVE_RE.match(path):
        if auth.is_staff(user):
            return await _proxy(SERVICING_URL, f"/{path}", request, user,
                                extra_headers=_principal_headers(svc, user))
        raise HTTPException(status_code=403, detail="staff only")

    if path == "reconciliation/peek":
        if auth.is_staff(user):
            return await _proxy(SERVICING_URL, f"/{path}", request, user, extra_headers=svc)
        raise HTTPException(status_code=403, detail="staff only")

    # The review queue (D22) and the disposition route on one of its items. The
    # client authorised the in-app queue as the ONLY destination for a flagged
    # payment, which makes this hop the whole delivery mechanism -- there is no
    # email or webhook fallback behind it, deliberately.
    #
    # Staff-only here, and unlike `reconciliation/peek` these carry a SIGNED
    # PRINCIPAL: the queue returns payment amounts for real loans, and a
    # disposition stores the reviewer's name. Servicing verifies the assertion
    # itself, so this check is defence in depth rather than the boundary -- a
    # caller that reaches servicing directly with the shared internal token is
    # refused there for having no verified human behind it.
    if path == "reconciliation/review-queue" or _REVIEW_DISPOSITION_RE.match(path):
        if auth.is_staff(user):
            return await _proxy(SERVICING_URL, f"/{path}", request, user,
                                extra_headers=_principal_headers(svc, user))
        raise HTTPException(status_code=403, detail="staff only")

    # Unrecognized /lss sub-path -- fail closed rather than proxy something no
    # authz rule above accounted for. This is what kept servicing's legacy
    # `POST /payments` duplicate unreachable from a browser or a staff session
    # for as long as it existed; that route has since been retired outright
    # (docs/DEBT.md D2), so the fall-through no longer has to carry it. It still
    # covers `apply-payment`, which payment-service calls servicing directly for
    # and which should never be reachable through this proxy at all.
    raise HTTPException(status_code=404, detail="not found")


# --- LOS sub-services (the decomposed origination estate). -------------------
# Origination calls these server-to-server during the application flow; they are
# also exposed here so the portal / ops tooling can reach each service directly.
# Like /los/*, the underwriting-flow services forward a session if one is present
# but do not require it (an applicant can apply without an account).

@app.api_route("/kyc/{path:path}", methods=["GET", "POST"])
async def kyc(path: str, request: Request, authorization: str | None = Header(None)):
    # Staff-only, same as /decision/* and /disclosure/* below, and for the same
    # reason -- with one extra step of history worth keeping, because getting
    # this wrong is what made the previous version of this change ineffective.
    #
    # The session used to be OPTIONAL here, matching /los/* on the reasoning that
    # an applicant can apply without an account. But /los/* is origination, which
    # gates its own sensitive routes; this proxy forwards straight to a service
    # whose only auth is the X-Internal-Token that THIS FUNCTION ATTACHES. An
    # anonymous caller therefore got the gateway to sign its request for it:
    #
    #     curl -X POST localhost:8000/kyc/kyc/check -d '{"applicant_id": 1, ...}'
    #     -> 200, and a kyc_checks row for applicant 1 with name_verified=true
    #
    # That is forged CIP evidence in the record BSA/AML relies on, reachable on
    # the one port deliberately published to the host. Removing kyc-service's own
    # 8003 mapping did not touch it: the token check it added is satisfied by the
    # gateway's own token, so the front door stayed open while the side door was
    # being locked. Confirmed by an adversarial review and reproduced live.
    #
    # The real anonymous path to CIP is POST /los/applications, where origination
    # derives applicant/application identity from the row it just wrote rather
    # than from the caller's body. This route exists only for staff/ops tooling
    # to inspect KYC directly, which is exactly what /decision/* and
    # /disclosure/* concluded for the same shape of service.
    user = _require_user(authorization)
    if not auth.is_staff(user):
        raise HTTPException(status_code=403, detail="staff only")

    # Review finding (high): staff-only was not enough, because this proxy
    # attaches the trusted token and kyc-service persists whatever applicant_id
    # the body names. So any CSR, underwriter or admin could POST /kyc/kyc/check
    # with an invented applicant and mint durable CIP evidence against a stranger
    # -- the same forgery the anonymous fix closed, now requiring only the
    # weakest staff role rather than no session at all.
    #
    # This route exists so staff and ops can INSPECT kyc-service. Inspection is
    # a read. The mutating endpoint has exactly one legitimate caller,
    # origination-service, which reaches it server-to-server and derives the
    # applicant from the row it has just written rather than from a request body.
    #
    # Read-only here, and kyc-service independently verifies the
    # application/applicant linkage before inserting -- neither control relies on
    # the other, because the token proves only where a request came from and
    # never that its contents are true.
    if request.method != "GET":
        raise HTTPException(
            status_code=405,
            detail=("kyc-service is read-only through the gateway; CIP runs as part "
                    "of POST /los/applications"),
        )
    return await _proxy(
        KYC_URL, f"/{path}", request, user,
        extra_headers={"X-Internal-Token": INTERNAL_SERVICE_TOKEN},
    )


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


#: The one assistant route whose work is traced end to end: the underwriting
#: summary. Closed alternation and anchored, like the servicing matchers above --
#: a prefix test would also match paths this service knows nothing about.
_ASSISTANT_SUMMARY_RE = re.compile(r"^applications/(\d+)/summary$")


@app.api_route("/assistant/{path:path}", methods=["GET", "POST"])
async def assistant(path: str, request: Request, authorization: str | None = Header(None)):
    # AI summary returns risk tier + internal flags — staff only, not the
    # borrower. (Policy Q&A is split out above -- no per-applicant financials there.)
    user = _require_user(authorization)
    if not auth.is_staff(user):
        raise HTTPException(status_code=403, detail="Forbidden")

    # The trace root opens HERE, and deliberately after the two lines above.
    #
    # The client asked to see the agent run from the authenticated entry point
    # onward. loan-assistant's trace began one hop downstream, so the step that
    # decides whether the agent runs at all -- this authorisation -- was not in
    # the picture. Opening the run after the check means a rejected request
    # produces no run, and a run that exists is one that was allowed.
    #
    # Only the summary route is traced; `/health` and anything else forwards
    # untraced rather than minting a run for work nobody asked to see.
    traced = bool(_ASSISTANT_SUMMARY_RE.match(path))
    trace_headers, root = ({}, None)
    if traced:
        trace_headers, root = agent_trace.start_root(
            role=str(user.get("role", "")), route_class="agent_summary")

    resp = await _proxy(LOAN_ASSISTANT_URL, f"/{path}", request, user,
                        extra_headers=trace_headers or None)
    agent_trace.finish_root(root, resp.status_code)
    return resp
