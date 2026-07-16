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


class DecisionPersistenceError(RuntimeError):
    """Could not durably record the decision + its audit event (decision_events).

    Raised instead of swallowed: a decision that can't be proven to have happened
    is exactly the gap the append-only audit trail exists to close, so returning
    a decision to the caller anyway would defeat the point of recording one at all.
    """


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


def _call_ai_scorer(bureau_score: int, application: dict) -> dict:
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
        body = resp.json()
        score = body.get("score")
        reason_codes = body.get("reason_codes")
        if score is None or reason_codes is None:
            # The vendor responded but left out a field we require. Guessing a
            # score of 0, or guessing a reason from the legacy bureau/income
            # formula, risks a fabricated score or a legally-required reason
            # that wasn't the model's actual driver — fail closed instead, same
            # as an unreachable model. (Review finding: this used to be
            # body["score"] directly, so a response missing *score* specifically
            # raised a raw KeyError here instead of this same clean error --
            # asymmetric with the reason_codes check right below it.)
            missing = [name for name, val in (("score", score), ("reason_codes", reason_codes)) if val is None]
            raise ModelUnavailableError(
                f"AI scorer response missing required field(s) {missing} — refusing "
                "to guess a score or an adverse-action reason from a formula the "
                "licensed model may not actually be using (it also weighs "
                "requested_amount/term_months, unlike the legacy bureau/income-only "
                "heuristic)."
            )
        return {
            "score": score,
            "model_version": AI_MODEL_VERSION,
            "reason_codes": reason_codes,
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
    """Score via the licensed AI scorer (or its dev/test stub), decide, and report
    the adverse-action reasons the scorer itself said actually drove the score down
    (see _call_ai_scorer — never re-derived locally for a real vendor response)."""
    time.sleep(0.05)  # stand-in for a slow scorecard pass on the request thread
    income = application.get("income", 0)
    scored = _call_ai_scorer(bureau_score, application)
    model_score = scored["score"]
    model_version = scored["model_version"]
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
    reason_codes = scored["reason_codes"] or [REASON_INSUFFICIENT_INCOME]
    return {
        "score": model_score,
        "decision": decision_outcome,
        "reason_codes": reason_codes,
        "model_version": model_version,
        "top_features": top_features,
    }


def decide(application: dict) -> dict:
    """Full synchronous decisioning chain. Persists the legacy outcome-only
    `decisions` row and the append-only `decision_events` row (inputs, model
    score/version, top features, reason codes) as ONE transaction — both land or
    neither does, so a decision is never returned to the caller without the audit
    row that proves it happened (review finding: the two used to be written
    separately with each failure only logged, letting a decision commit with no
    matching audit event when the second insert failed silently)."""
    bureau_score = _pull_credit(application.get("ssn", ""))
    result = _run_model(bureau_score, application)
    app_id = application.get("app_id")

    try:
        db.transaction([
            (
                "INSERT INTO decisions (app_id, outcome) VALUES (%s, %s) "
                "ON CONFLICT (app_id) DO UPDATE SET outcome = EXCLUDED.outcome",
                (app_id, result["decision"]),
            ),
            (
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
            ),
        ])
    except Exception as e:
        log.error("could not persist decision + decision_event: %s", e)
        raise DecisionPersistenceError(
            f"app_id={app_id}: decision computed ({result['decision']}, score="
            f"{result['score']}) but could not be durably recorded — refusing to "
            "report an outcome with no matching audit trail."
        ) from e

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
