"""Credit decisioning.

This logic was lifted verbatim out of the origination service into its own
decision-service — the behaviour (and the debt) is unchanged by the split.

Week 3: the AI scorer call now has the same fail-closed contract as the bureau call
(ModelUnavailableError), adverse-action reasons are mapped to whichever input actually
drove the score down instead of a fixed nearest-checkbox string, and every decision
persists an append-only `decision_events` row (inputs, model score/version, top
features, reason codes) — the dispute-proof record Reg B requires and the legacy
outcome-only `decisions` table never had.

Async rework (adr/0006): the credit pull, the bureau call, and the model run used to
be a synchronous chain executed inline on the request thread -- decision-service's
own thread pool (FastAPI's default for sync `def` routes) exhausted above ~20
concurrent applications, since each one held a worker thread for the full duration
of two blocking, up-to-30s vendor HTTP calls. Both outbound calls now use
httpx.AsyncClient and the whole chain (`_pull_credit` -> `_run_model` -> `decide`)
is async, so a request waiting on Experian or the AI scorer frees the thread pool
entirely -- the event loop's own async I/O handles many more concurrent in-flight
vendor calls than a fixed-size thread pool ever could. The DB write in `decide()`
stays a synchronous psycopg2 call (a fast local Postgres INSERT, not the external-
vendor bottleneck this rework targets) -- a fully async DB layer (asyncpg) is a
separate, larger change, not done here. Scope note: this fixes decision-service's
OWN internal chain only; origination-service's own call INTO decision-service
(services/origination-service/app/clients.py) is still synchronous -- that's a
different service's own thread-pool budget, out of scope for this fix.
"""
import asyncio

import httpx
from pydantic import BaseModel, Field, ValidationError

from .config import (
    AI_MODEL_API_KEY,
    AI_MODEL_BASE_URL,
    AI_MODEL_VERSION,
    ALLOW_CREDIT_STUB,
    ALLOW_MODEL_STUB,
    ENVIRONMENT,
    EXPERIAN_BASE_URL,
    EXPERIAN_KEY,
)
from .logging_config import get_logger
from . import bureau, db

log = get_logger("decision")

# Specific, per-applicant adverse-action reasons — see _reason_codes() for which
# input drives which reason. Replaces the old single hardcoded "purchasing history"
# string that never reflected what the model actually weighed.
#: Approved consumer-facing wording, keyed by the reason code the model
#: reported. Spec 0003 §1.6.
#:
#: A reason code is authoritative about **the model**. It is not automatically
#: authoritative **wording**, and the two are different artefacts: the code is
#: audit evidence and is retained verbatim in `decision_events`; this table
#: produces what a declined applicant is actually told.
#:
#: The deterministic stub's two codes ARE approved sentences, so they map to
#: themselves. That is not a placeholder -- their meaning is owned by
#: `_reason_codes` in this repository rather than by a third party, which is
#: exactly what makes them safe to show. Two entries is not a defect where two
#: drivers is what the stub genuinely has.
#:
#: **Real vendor codes are deliberately absent.** No vendor taxonomy or approved
#: wording is committed anywhere in this repository, so any entry added here for
#: a real code would be invented semantics. `high_debt_to_income` appears in
#: this repo only as a test author's placeholder and MUST NOT be added.
#: VENDOR-BLOCKED (spec 0003, *Blocked*).
APPROVED_CONSUMER_REASONS: dict[str, str] = {}


REASON_LOW_BUREAU_SCORE = "Low credit bureau score relative to lending criteria"
REASON_INSUFFICIENT_INCOME = "Insufficient income relative to lending criteria"

# Baseline "healthy applicant" values used only to compare which input is further
# below a reasonable bar — not approval thresholds themselves. See _reason_codes().
APPROVED_CONSUMER_REASONS.update({
    REASON_LOW_BUREAU_SCORE: REASON_LOW_BUREAU_SCORE,
    REASON_INSUFFICIENT_INCOME: REASON_INSUFFICIENT_INCOME,
})


_HEALTHY_BUREAU_SCORE = 720
_HEALTHY_INCOME = 50_000


class CreditBureauUnavailableError(RuntimeError):
    """Bureau not configured/reachable and stubbing isn't allowed in this environment."""


class ModelUnavailableError(RuntimeError):
    """Licensed AI scorer not configured/reachable and stubbing isn't allowed here."""



class UnmappedAdverseActionReason(ModelUnavailableError):
    """The model reported a reason with no approved consumer wording.

    Fails closed, and subclasses ModelUnavailableError so it inherits the
    existing refusal handling rather than inventing a second one -- while
    staying a distinct class, because "the scorer is unreachable" and "the
    scorer answered with something we may not repeat to an applicant" are
    different incidents and a log that conflated them would mislead whoever
    reads it.

    Refusing the whole decision is deliberate and is the strict reading of
    12 CFR 1002.9: a denial has to carry a statement of specific reasons, so a
    denial we cannot lawfully explain is not a decision worth committing. The
    alternatives were all worse -- a nearest match invents a reason the model
    did not give, a generic fallback is the `GENERIC_REASONS` defect this
    repository already removed once, and passing the raw code through puts a
    machine token in front of a person.

    Operationally severe with a real vendor and an empty mapping table: every
    denial refuses until approved wording exists. That is the honest posture,
    and spec 0003 records it rather than softening it.
    """


class _ScorerResponse(BaseModel):
    """Strict schema for a real vendor scorer response.

    Review finding: the old check only confirmed score/reason_codes were present
    (not None) -- never that they were the right *type*. A vendor drift like
    reason_codes: "high_debt_to_income" (a string, not a list) is truthy, so it
    sailed through: persisted to decision_events as-is, adverse_action_reason
    became the string's first character ("h"), and the router's
    DecisionOut(reason_codes=...) then failed response validation -- after the
    decision_events row had already committed. Validating the full shape here,
    before _run_model() touches the payload and before any DB write, means a
    malformed vendor response fails closed with ModelUnavailableError instead of
    committing a malformed audit event first.

    score is bounded to a generous 0-1000 range (loose enough to cover a
    licensed model's own scale, which needn't match a traditional 300-850 bureau
    score) purely to catch garbage -- negative values, NaN/Infinity, or an
    obviously wrong magnitude -- not to encode a real business threshold.
    """

    score: float = Field(ge=0, le=1000, allow_inf_nan=False)
    reason_codes: list[str]


def _stub_score(ssn: str) -> int:
    return 680 if ssn and ssn[-1] in "02468" else 612


async def _pull_credit(ssn: str, request_key: str) -> bureau.BureauResult:
    """Async bureau call, through the bureau.BureauClient seam.

    PR #6 review (Gap A): `request_key` is origination's idempotency key for
    this logical decision request. It is stable across a retry that follows an
    ambiguous timeout and different for a genuinely new decision request, so a
    retry recovers the original pull instead of billing a second hard inquiry
    against the applicant. The SSN travels in a POST body, never a query
    string -- see bureau.py for the full contract and its honest limitation.

    Returns a BureauResult (score + non-sensitive provider reference id)
    rather than a bare int, so the reference can be persisted for later
    lookup without keeping any part of the raw provider response.
    """
    if not EXPERIAN_KEY:
        if not ALLOW_CREDIT_STUB:
            raise CreditBureauUnavailableError(
                f"EXPERIAN_KEY is not set (ENVIRONMENT={ENVIRONMENT!r}) — refusing to "
                "decide from a fake credit score outside development/test."
            )
        log.warning("EXPERIAN_KEY not set — using deterministic dev stub score")
        return await bureau.stub_client.pull_score(ssn, request_key)

    try:
        return await bureau.HttpBureauClient().pull_score(ssn, request_key)
    except Exception:
        if not ALLOW_CREDIT_STUB:
            raise
        # Deterministic stub so the demo runs without a live bureau. Same
        # request_key, so a retry still collapses onto one stub operation.
        return await bureau.stub_client.pull_score(ssn, request_key)


def _stub_model_score(bureau_score: int, income: float) -> int:
    """Deterministic stand-in for the licensed scorer, dev/test only. Mirrors the
    legacy rules scorecard's own math so existing fixtures/expectations still hold."""
    return int(bureau_score * 0.9 + (income / 1000))


async def _call_ai_scorer(bureau_score: int, application: dict) -> dict:
    """Call the newly licensed AI credit-scoring model. Same fail-closed contract as
    _pull_credit: a missing/unreachable licensed model must not silently score from
    fake data outside dev/test.

    Returns {"score", "model_version", "reason_codes"}. reason_codes must come from
    the vendor itself when a real call succeeds, not from _reason_codes()'s legacy
    bureau/income shortfall formula — the licensed model also sees requested_amount
    and term_months and may weight them, so a locally-guessed reason could name a
    driver that isn't actually why the real model scored this applicant the way it
    did (review finding). A real response that omits reason_codes fails closed
    (ModelUnavailableError) rather than falling back to that guess. The deterministic
    dev/test stub is the one case _reason_codes() is authoritative for, since the
    stub's score IS computed by that exact bureau/income formula (_stub_model_score)."""
    income = application.get("income", 0)

    if not AI_MODEL_API_KEY:
        if not ALLOW_MODEL_STUB:
            raise ModelUnavailableError(
                f"AI_MODEL_API_KEY is not set (ENVIRONMENT={ENVIRONMENT!r}) — refusing "
                "to score from a fake model outside development/test."
            )
        log.warning("AI_MODEL_API_KEY not set — using deterministic dev stub score")
        return {
            "score": _stub_model_score(bureau_score, income),
            "model_version": f"{AI_MODEL_VERSION}-stub",
            "reason_codes": _reason_codes(bureau_score, income),
        }

    try:
        # structured like a real call; in dev/test there's no live vendor endpoint.
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{AI_MODEL_BASE_URL}/score",
                json={
                    "bureau_score": bureau_score,
                    "income": income,
                    "requested_amount": application.get("requested_amount"),
                    "term_months": application.get("term_months"),
                },
                headers={"Authorization": f"Bearer {AI_MODEL_API_KEY}"},
            )
        resp.raise_for_status()
        body = resp.json()
        try:
            validated = _ScorerResponse.model_validate(body)
        except ValidationError as e:
            # The vendor responded but the payload doesn't match the required
            # shape — missing field, wrong type (e.g. reason_codes as a bare
            # string instead of a list), or a score outside a sane range.
            # Guessing a score, or guessing a reason from the legacy
            # bureau/income formula, risks a fabricated score or a
            # legally-required reason that wasn't the model's actual driver —
            # fail closed instead, same as an unreachable model, and before
            # anything reaches the database.
            raise ModelUnavailableError(
                f"AI scorer response failed validation: {e} — refusing to guess "
                "a score or an adverse-action reason from a formula the "
                "licensed model may not actually be using (it also weighs "
                "requested_amount/term_months, unlike the legacy bureau/income-only "
                "heuristic)."
            ) from e
        return {
            "score": validated.score,
            "model_version": AI_MODEL_VERSION,
            "reason_codes": validated.reason_codes,
        }
    except ModelUnavailableError:
        raise
    except Exception:
        if not ALLOW_MODEL_STUB:
            raise
        return {
            "score": _stub_model_score(bureau_score, income),
            "model_version": f"{AI_MODEL_VERSION}-stub",
            "reason_codes": _reason_codes(bureau_score, income),
        }


def consumer_adverse_action_reason(reason_codes, decision: str):
    """The approved sentence a declined applicant is told, or refuse.

    Spec 0003 §1.1/§1.4/§1.6. Returns None for any outcome that is not a
    denial -- an approval has no adverse action to explain.

    This is the seam that stops `reason_codes[0]` becoming consumer text by
    default, which is what `graph.py::_node_finalize` used to do.
    """
    if decision != "deny":
        return None

    codes = [c for c in (reason_codes or []) if isinstance(c, str) and c.strip()]
    if not codes:
        raise UnmappedAdverseActionReason(
            "the model reported no usable reason code for a denial; refusing "
            "rather than issuing an unexplained adverse action"
        )

    code = codes[0]
    try:
        return APPROVED_CONSUMER_REASONS[code]
    except KeyError:
        # The code itself is NOT interpolated into the message: it is model
        # output, it reaches logs through this exception, and the whole point
        # of this function is that it is not fit to be repeated onward. The
        # count tells an operator how much is unmapped without quoting any of
        # it.
        raise UnmappedAdverseActionReason(
            f"the model reported {len(codes)} reason code(s), none of which "
            f"has approved consumer wording; refusing the denial"
        ) from None


def _reason_codes(bureau_score: int, income: float) -> list[str]:
    """Which input actually pulled the score down — Reg B's "specific principal
    reason," not a fixed nearest-checkbox string. Compares each factor's shortfall
    from a healthy baseline (not an approval threshold); whichever shortfall is
    larger is the principal driver of the low score."""
    bureau_shortfall = max(0.0, (_HEALTHY_BUREAU_SCORE - bureau_score) * 0.9)
    income_shortfall = max(0.0, (_HEALTHY_INCOME - income) / 1000)
    if bureau_shortfall == 0 and income_shortfall == 0:
        return []
    if bureau_shortfall >= income_shortfall:
        return [REASON_LOW_BUREAU_SCORE]
    return [REASON_INSUFFICIENT_INCOME]


async def _run_model(bureau_score: int, application: dict) -> dict:
    """Score via the licensed AI scorer (or its dev/test stub), decide, and report
    the adverse-action reasons the scorer itself said actually drove the score down
    (see _call_ai_scorer — never re-derived locally for a real vendor response)."""
    await asyncio.sleep(0.05)  # stand-in for a slow scorecard pass -- asyncio.sleep,
                               # not time.sleep, so this doesn't block the event loop
    income = application.get("income", 0)
    scored = await _call_ai_scorer(bureau_score, application)
    model_score = scored["score"]
    model_version = scored["model_version"]
    is_stub = model_version.endswith("-stub")

    # The bureau/income "contribution" formula is the *stub's own scoring math*
    # (_stub_model_score) -- authoritative for the stub, but never returned by
    # the real vendor (_ScorerResponse only has score/reason_codes, no feature
    # attributions). Persisting it for a real response would claim bureau/income
    # drove a decision the licensed model may have made on requested_amount/
    # term_months instead -- fabricated audit data, same failure mode as the
    # reason_codes gap below. Record null rather than guess.
    top_features = (
        {
            "bureau_score": bureau_score,
            "income": income,
            "bureau_contribution": round(bureau_score * 0.9, 2),
            "income_contribution": round(income / 1000, 2),
        }
        if is_stub
        else None
    )

    if model_score >= 660:
        return {
            "score": model_score,
            "decision": "approve",
            "reason_codes": [],
            "model_version": model_version,
            "top_features": top_features,
        }

    decision_outcome = "deny" if model_score < 600 else "refer"
    reason_codes = scored["reason_codes"]
    if not reason_codes:
        if is_stub:
            # The dev/test stub score IS computed by the bureau/income formula
            # (_stub_model_score), so _reason_codes() is authoritative here.
            reason_codes = _reason_codes(bureau_score, income)
        else:
            # Real vendor call succeeded with a sub-660 score but no reason_codes.
            # Filling in a locally-guessed reason (e.g. REASON_INSUFFICIENT_INCOME)
            # would persist an adverse-action reason the licensed model never
            # actually gave -- an audit/compliance failure. Fail closed instead.
            raise ModelUnavailableError(
                f"AI scorer returned score={model_score} (<660) with empty "
                "reason_codes -- refusing to fabricate an adverse-action reason "
                "the licensed model never gave."
            )
    return {
        "score": model_score,
        "decision": decision_outcome,
        "reason_codes": reason_codes,
        "model_version": model_version,
        "top_features": top_features,
    }


async def decide(application: dict) -> dict:
    """Full decisioning chain (async -- see module docstring). Compute-only:
    pulls the bureau score, runs the scoring model, and returns the result
    (score, decision, reason_codes, bureau_score, model_version,
    top_features) -- it persists nothing to the database at all (PR #6
    review, Finding 2). origination-service is the sole writer of both
    `decisions` and `decision_events`, atomically, only after its own
    lock+recheck confirms the request that triggered this call actually
    wins its finality race (see routers/applications.py::run_decision on
    the origination-service side).

    Week 3: the pull-credit / score / finalize steps are an explicit
    LangGraph graph (app/graph.py) instead of inline code here -- same
    three calls, same fail-closed exceptions, now individually traceable.
    Deferred import: graph.py imports this module at its own load time, so
    importing it up top would be circular; by the time decide() is
    actually called, this module has finished loading and the import below
    is just a sys.modules lookup.
    """
    from .graph import run as _run_graph

    return await _run_graph(application)
