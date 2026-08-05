"""
Production LLM client for loan application summarization.

Guardrails built in:
  1. Field allowlist   — only non-identifying underwriting fields ever reach the
                         prompt (amount, term, purpose, employment); applicant
                         name/email/phone/address are never sent to Anthropic at all
  2. PII/PCI redaction  — SSN, PAN, CVV stripped from whatever IS sent, as defense
                         in depth (e.g. free-text purpose/employer fields)
  3. Timeout           — 20 s hard wall; raises LLMTimeoutError
  4. Retry             — 3 attempts, exponential backoff, on 5xx / network errors only
  5. Cost guard        — refuses input token estimate above MAX_INPUT_TOKENS
  6. Structured output — returns validated LoanSummary (Pydantic); never raw text
  7. Redacted logging  — logs show redacted payload; raw values never appear

The LLM is never told the applicant's name — summarize_application() fills
applicant_name into the final LoanSummary from the trusted app_data directly, not
from anything the model returns. The model can't leak, mis-remember, or invent a
name it was never given.

Never bypass the redactor before calling summarize_application().
Never increase MAX_INPUT_TOKENS without a cost-review sign-off.
"""

import json
import logging
import os
import re
import time
from typing import Any, Literal

import anthropic
import httpx
from pydantic import BaseModel, Field
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from . import config
from .redactor import redact_dict, redact_str
from .schemas import LoanSummary

try:
    from langsmith.wrappers import wrap_anthropic
except ImportError:  # pragma: no cover -- langsmith is a real dependency now,
    # but tracing must never be a hard requirement to serve a real request.
    wrap_anthropic = None

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_INPUT_TOKENS = 2_000   # cost guard — raise only with explicit sign-off
MAX_OUTPUT_TOKENS = 500    # summary should be short
TIMEOUT_SECONDS = 20.0

# Only these top-level application fields are eligible for the prompt. Contact
# identifiers (name, email, phone, address) are deliberately excluded — a risk
# summary doesn't need them, and they have no business reaching a third-party API.
# income/employment_years are required here: the system prompt instructs the model
# to judge risk_tier from DTI and employment length, so those facts must actually
# reach it — without them the model was inventing a risk chip from data it never
# saw (Codex review on PR #2).
_PROMPT_ALLOWED_FIELDS = (
    "amount", "term_months", "purpose", "income", "employer", "job_title", "employment_years",
)
# risk_tier/flags require these two to be present and non-null — see
# _has_risk_grounding_data() below.
_RISK_GROUNDING_FIELDS = ("income", "employment_years")

_SYSTEM = """
You are a loan officer assistant at Meridian Lending. Given loan application details,
produce a concise, factual summary in the exact JSON schema provided.

Rules:
- NEVER include SSN, full card numbers, or CVV in your output.
- Use only the data provided — do not invent information.
- risk_tier: 'low' if DTI<30% and employment>2yr; 'high' if DTI>50% or employment<6mo;
  'decline' if income cannot plausibly service the loan; else 'medium'.
- flags: list specific concerns (e.g. "Employment < 1 year", "Loan-to-income ratio > 4x").
  Empty list if none.
""".strip()


class _LLMOutput(BaseModel):
    """What the model is actually asked to produce — no applicant_name. That field
    is filled in by summarize_application() from trusted server-side data instead."""

    loan_amount: float = Field(description="Requested loan amount in USD")
    term_months: int = Field(description="Loan term in months")
    purpose: str = Field(description="Stated purpose of the loan")
    risk_tier: Literal["low", "medium", "high", "decline"]
    summary: str = Field(description="2-3 sentence plain-English summary. No PAN, CVV, or SSN.")
    flags: list[str] = Field(default_factory=list)


class LLMTimeoutError(Exception):
    pass


class LLMCostGuardError(Exception):
    pass


class LLMResponseError(Exception):
    pass


class LLMInsufficientDataError(Exception):
    """Raised when the source application is missing the data risk_tier depends on
    (income, employment_years — both nullable columns). Fails loudly instead of
    letting the model invent a risk chip from data it never saw."""


class LLMConfigError(Exception):
    """LLM_PROVIDER is misconfigured -- fail loudly rather than call the wrong
    vendor or call Bedrock with no model id."""


_VALID_LLM_PROVIDERS = {"anthropic", "bedrock"}


def _check_provider() -> None:
    """Fail closed on an unrecognized LLM_PROVIDER rather than silently
    guessing. Review finding: the previous `if == "bedrock" else <direct API>`
    shape treated *any* other value -- including a typo like "berock" -- as
    "anthropic", which could silently send prompts to the wrong vendor (or
    just produce a confusing "ANTHROPIC_API_KEY not set" error) instead of a
    clear, immediate configuration error naming the actual problem."""
    if config.LLM_PROVIDER not in _VALID_LLM_PROVIDERS:
        raise LLMConfigError(
            f"LLM_PROVIDER={config.LLM_PROVIDER!r} is not a recognized provider "
            f"-- expected one of {sorted(_VALID_LLM_PROVIDERS)}."
        )


def make_client() -> "anthropic.Anthropic | anthropic.AnthropicBedrock":
    """Build the LLM client for the configured provider. Both client classes
    expose the same .messages.create(...) interface, so nothing else in this
    module needs to know which one it got."""
    _check_provider()
    if config.LLM_PROVIDER == "bedrock":
        # AnthropicBedrock() with no arguments reads AWS_BEARER_TOKEN_BEDROCK
        # (or the standard AWS credential chain) and AWS_REGION from the
        # environment automatically -- confirmed against the installed SDK
        # (anthropic/lib/bedrock/_client.py), no extra credential code needed.
        client = anthropic.AnthropicBedrock()
    else:
        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError("ANTHROPIC_API_KEY not set")
        client = anthropic.Anthropic(api_key=api_key)

    # wrap_anthropic() patches .messages.create in place and returns the same
    # object -- confirmed against the installed SDK -- and its patched call is
    # a no-op passthrough unless LANGSMITH_TRACING/LANGSMITH_API_KEY are set in
    # the environment, so this is safe to always attempt. Tracing is
    # observability, not a compliance guardrail (unlike EXPERIAN_KEY/
    # AI_MODEL_API_KEY in decision-service) -- a missing package or bad
    # LangSmith config must never block a real call, so this fails open.
    if wrap_anthropic is not None:
        try:
            client = wrap_anthropic(client)
        except Exception as exc:
            log.warning("could not wrap client for LangSmith tracing: %s", exc)
    return client


def _model_id() -> str:
    _check_provider()
    if config.LLM_PROVIDER == "bedrock":
        if not config.BEDROCK_MODEL_ID:
            raise LLMConfigError(
                "LLM_PROVIDER=bedrock but BEDROCK_MODEL_ID is not set -- set it "
                "to a Claude model id your AWS account has Bedrock access to."
            )
        return config.BEDROCK_MODEL_ID
    return MODEL


def _has_risk_grounding_data(app_data: dict) -> bool:
    return all(app_data.get(field) is not None for field in _RISK_GROUNDING_FIELDS)


def _missing_risk_grounding_fields(app_data: dict) -> list[str]:
    return [field for field in _RISK_GROUNDING_FIELDS if app_data.get(field) is None]


def _build_prompt(app_data: dict) -> str:
    allowed = {k: app_data.get(k) for k in _PROMPT_ALLOWED_FIELDS if app_data.get(k) is not None}
    safe = redact_dict(allowed)
    return (
        "Summarize this loan application as JSON with fields: loan_amount, "
        "term_months, purpose, risk_tier, summary, flags.\n\n"
        f"Application data:\n{json.dumps(safe, indent=2)}\n\n"
        "Respond with only the JSON object — no markdown fences, no extra text."
    )


def _estimate_tokens(text: str) -> int:
    # rough: 1 token ≈ 4 chars
    return len(text) // 4


_OPEN_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?")
_CLOSE_FENCE_RE = re.compile(r"\n?```\s*$")


def strip_markdown_fences(raw: str) -> str:
    """Some models wrap structured output in a ```json fence despite the prompt
    explicitly saying not to (confirmed live: a real Bedrock Claude response did
    exactly this) -- strip it defensively rather than relying solely on prompt
    compliance, same defense-in-depth principle as the redactor running even
    though only allowlisted fields reach the prompt in the first place.

    Review finding on the first version: it matched the open fence, a mandatory
    newline, content, another mandatory newline, then the close fence as ONE
    rigid pattern -- a real response with no newline immediately before the
    closing ``` (a perfectly normal way to fence JSON) silently failed to match
    at all, leaving the fence in place for json.loads() to then choke on. Fixed
    by stripping the open and close markers independently, each with an
    optional trailing/leading newline, so the exact newline placement around
    the closing fence no longer matters."""
    text = raw.strip()
    if not text.startswith("```"):
        return text
    text = _OPEN_FENCE_RE.sub("", text, count=1)
    text = _CLOSE_FENCE_RE.sub("", text, count=1)
    return text.strip()


@retry(
    retry=retry_if_exception_type((anthropic.APIStatusError, httpx.NetworkError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
def call_api(
    client: "anthropic.Anthropic | anthropic.AnthropicBedrock",
    prompt: str,
    system: str = _SYSTEM,
) -> str:
    try:
        msg = client.messages.create(
            model=_model_id(),
            max_tokens=MAX_OUTPUT_TOKENS,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            timeout=TIMEOUT_SECONDS,
        )
        return msg.content[0].text
    except anthropic.APITimeoutError as exc:
        raise LLMTimeoutError(f"LLM call timed out after {TIMEOUT_SECONDS}s") from exc


def summarize_application(app_data: dict[str, Any]) -> LoanSummary:
    """
    Summarize a loan application for a loan officer.

    app_data: raw application dict from the LOS (may contain SSN, income, etc.)
    Returns: validated LoanSummary — safe to display, safe to log.
    Raises: LLMCostGuardError, LLMTimeoutError, LLMResponseError, LLMInsufficientDataError
    """
    if not _has_risk_grounding_data(app_data):
        # Bug fix: this used to always name BOTH _RISK_GROUNDING_FIELDS
        # regardless of which one was actually missing -- a staff reviewer
        # reading "is missing ('income', 'employment_years')" when income was
        # right there in the application would reasonably believe income
        # data was lost, not just employment_years. Name only what's
        # actually absent.
        raise LLMInsufficientDataError(
            f"app_id={app_data.get('id', 'unknown')} is missing "
            f"{_missing_risk_grounding_fields(app_data)} — refusing to let the "
            "model assign a risk_tier it has no data to support."
        )

    prompt = _build_prompt(app_data)

    estimated = _estimate_tokens(_SYSTEM + prompt)
    if estimated > MAX_INPUT_TOKENS:
        raise LLMCostGuardError(
            f"Estimated input tokens ({estimated}) exceeds guard ({MAX_INPUT_TOKENS}). "
            "Trim the application payload before calling summarize_application()."
        )

    log.info(
        "llm_client summarize app_id=%s estimated_tokens=%d",
        app_data.get("id", "unknown"),
        estimated,
    )

    client = make_client()

    t0 = time.monotonic()
    raw = call_api(client, prompt)
    elapsed = time.monotonic() - t0

    log.info(
        "llm_client response app_id=%s elapsed_ms=%d",
        app_data.get("id", "unknown"),
        int(elapsed * 1000),
    )

    try:
        data = json.loads(strip_markdown_fences(raw))
        llm_output = _LLMOutput(**data)
    except Exception as exc:
        safe_raw = redact_str(raw)
        log.error("llm_client parse error response=%s", safe_raw)
        raise LLMResponseError(f"Could not parse LLM response: {exc}") from exc

    return LoanSummary(applicant_name=_applicant_name(app_data), **llm_output.model_dump())


def _applicant_name(app_data: dict) -> str:
    """Applicant name for the response, taken from trusted server-side data — the
    LLM is never given it, so it can't be the source of this field."""
    applicant = app_data.get("applicant")
    if isinstance(applicant, dict):
        return applicant.get("name") or "Applicant"
    if isinstance(applicant, str) and applicant:
        return applicant
    return app_data.get("applicant_name") or "Applicant"
