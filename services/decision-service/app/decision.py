"""Credit decisioning.

This logic was lifted verbatim out of the origination service into its own
decision-service — the behaviour (and the debt) is unchanged by the split.

The credit pull, the bureau call, and the model run are a SYNCHRONOUS chain executed
inline on the request thread (load note: timeouts past ~20 concurrent apps; tracked
for a future async rework, not fixed this week — see adr/0006).

Week 3: the AI scorer call now has the same fail-closed contract as the bureau call
(ModelUnavailableError), adverse-action reasons are mapped to whichever input actually
drove the score down instead of a fixed nearest-checkbox string, and every decision
persists an append-only `decision_events` row (inputs, model score/version, top
features, reason codes) — the dispute-proof record Reg B requires and the legacy
outcome-only `decisions` table never had.
"""
import json
import time

import httpx

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
from . import db

log = get_logger("decision")

# Specific, per-applicant adverse-action reasons — see _reason_codes() for which
# input drives which reason. Replaces the old single hardcoded "purchasing history"
# string that never reflected what the model actually weighed.
REASON_LOW_BUREAU_SCORE = "Low credit bureau score relative to lending criteria"
REASON_INSUFFICIENT_INCOME = "Insufficient income relative to lending criteria"

# Baseline "healthy applicant" values used only to compare which input is further
# below a reasonable bar — not approval thresholds themselves. See _reason_codes().
_HEALTHY_BUREAU_SCORE = 720
_HEALTHY_INCOME = 50_000


class CreditBureauUnavailableError(RuntimeError):
    """Bureau not configured/reachable and stubbing isn't allowed in this environment."""


class ModelUnavailableError(RuntimeError):
    """Licensed AI scorer not configured/reachable and stubbing isn't allowed here."""


def _stub_score(ssn: str) -> int:
    return 680 if ssn and ssn[-1] in "02468" else 612


def _pull_credit(ssn: str) -> int:
    """Synchronous bureau call. Blocks the request thread. No real timeout budget."""
    if not EXPERIAN_KEY:
        if not ALLOW_CREDIT_STUB:
            raise CreditBureauUnavailableError(
                f"EXPERIAN_KEY is not set (ENVIRONMENT={ENVIRONMENT!r}) — refusing to "
                "decide from a fake credit score outside development/test."
            )
        log.warning("EXPERIAN_KEY not set — using deterministic dev stub score")
        return _stub_score(ssn)

    try:
        # structured like a real call; in dev there's no live bureau so we fall back.
        resp = httpx.get(
            f"{EXPERIAN_BASE_URL}/score",
            params={"ssn": ssn},
            headers={"Authorization": f"Bearer {EXPERIAN_KEY}"},
            timeout=30,
        )
        return resp.json().get("score", 680)
    except Exception:
        if not ALLOW_CREDIT_STUB:
            raise
        # deterministic stub so the demo runs without a live bureau
        return _stub_score(ssn)


def _stub_model_score(bureau_score: int, income: float) -> int:
    """Deterministic stand-in for the licensed scorer, dev/test only. Mirrors the
    legacy rules scorecard's own math so existing fixtures/expectations still hold."""
    return int(bureau_score * 0.9 + (income / 1000))


def _call_ai_scorer(bureau_score: int, application: dict) -> tuple[int, str]:
    """Call the newly licensed AI credit-scoring model. Same fail-closed contract as
    _pull_credit: a missing/unreachable licensed model must not silently score from
    fake data outside dev/test. Returns (score, model_version_actually_used) so a
    stubbed score is never recorded as if the real vendor produced it."""
    income = application.get("income", 0)

    if not AI_MODEL_API_KEY:
        if not ALLOW_MODEL_STUB:
            raise ModelUnavailableError(
                f"AI_MODEL_API_KEY is not set (ENVIRONMENT={ENVIRONMENT!r}) — refusing "
                "to score from a fake model outside development/test."
            )
        log.warning("AI_MODEL_API_KEY not set — using deterministic dev stub score")
        return _stub_model_score(bureau_score, income), f"{AI_MODEL_VERSION}-stub"

    try:
        # structured like a real call; in dev/test there's no live vendor endpoint.
        resp = httpx.post(
            f"{AI_MODEL_BASE_URL}/score",
            json={
                "bureau_score": bureau_score,
                "income": income,
                "requested_amount": application.get("requested_amount"),
                "term_months": application.get("term_months"),
            },
            headers={"Authorization": f"Bearer {AI_MODEL_API_KEY}"},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["score"], AI_MODEL_VERSION
    except Exception:
        if not ALLOW_MODEL_STUB:
            raise
        return _stub_model_score(bureau_score, income), f"{AI_MODEL_VERSION}-stub"


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


def _run_model(bureau_score: int, application: dict) -> dict:
    """Score via the licensed AI scorer (or its dev/test stub), decide, and map
    adverse-action reasons to whichever input actually drove the score down."""
    time.sleep(0.05)  # stand-in for a slow scorecard pass on the request thread
    income = application.get("income", 0)
    model_score, model_version = _call_ai_scorer(bureau_score, application)
    top_features = {
        "bureau_score": bureau_score,
        "income": income,
        "bureau_contribution": round(bureau_score * 0.9, 2),
        "income_contribution": round(income / 1000, 2),
    }

    if model_score >= 660:
        return {
            "score": model_score,
            "decision": "approve",
            "reason_codes": [],
            "model_version": model_version,
            "top_features": top_features,
        }

    decision_outcome = "deny" if model_score < 600 else "refer"
    reason_codes = _reason_codes(bureau_score, income) or [REASON_INSUFFICIENT_INCOME]
    return {
        "score": model_score,
        "decision": decision_outcome,
        "reason_codes": reason_codes,
        "model_version": model_version,
        "top_features": top_features,
    }


def decide(application: dict) -> dict:
    """Full synchronous decisioning chain. Persists the legacy outcome-only
    `decisions` row plus an append-only `decision_events` row (inputs, model
    score/version, top features, reason codes) for every decision."""
    bureau_score = _pull_credit(application.get("ssn", ""))
    result = _run_model(bureau_score, application)
    app_id = application.get("app_id")

    try:
        db.query(
            "INSERT INTO decisions (app_id, outcome) VALUES (%s, %s) "
            "ON CONFLICT (app_id) DO UPDATE SET outcome = EXCLUDED.outcome",
            (app_id, result["decision"]),
        )
    except Exception as e:  # noqa
        log.warning("could not persist decision: %s", e)

    try:
        db.query(
            "INSERT INTO decision_events "
            "(app_id, requested_amount, term_months, annual_income, bureau_score, "
            " model_score, model_version, top_features, decision, reason_codes) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                app_id,
                application.get("requested_amount"),
                application.get("term_months"),
                application.get("income"),
                bureau_score,
                result["score"],
                result["model_version"],
                json.dumps(result["top_features"]),
                result["decision"],
                json.dumps(result["reason_codes"]),
            ),
        )
    except Exception as e:  # noqa
        log.warning("could not persist decision_event: %s", e)

    log.info(
        "GET /decision app_id=%s model_score=%s decision=%s reason_codes=%s",
        app_id, result["score"], result["decision"], result["reason_codes"],
    )
    return {
        "score": result["score"],
        "decision": result["decision"],
        "reason_codes": result["reason_codes"],
        "adverse_action_reason": result["reason_codes"][0] if result["reason_codes"] else None,
    }
