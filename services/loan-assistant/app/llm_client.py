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

from .redactor import redact_dict, redact_str
from .schemas import LoanSummary

log = logging.getLogger(__name__)

MODEL = "claude-sonnet-4-6"
MAX_INPUT_TOKENS = 2_000   # cost guard — raise only with explicit sign-off
MAX_OUTPUT_TOKENS = 500    # summary should be short
TIMEOUT_SECONDS = 20.0

# Only these top-level application fields are eligible for the prompt. Contact
# identifiers (name, email, phone, address) are deliberately excluded — a risk
# summary doesn't need them, and they have no business reaching a third-party API.
_PROMPT_ALLOWED_FIELDS = ("amount", "term_months", "purpose", "employer", "job_title")

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


@retry(
    retry=retry_if_exception_type((anthropic.APIStatusError, httpx.NetworkError)),
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=8),
    reraise=True,
)
def _call_api(client: anthropic.Anthropic, prompt: str) -> str:
    try:
        msg = client.messages.create(
            model=MODEL,
            max_tokens=MAX_OUTPUT_TOKENS,
            system=_SYSTEM,
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
    Raises: LLMCostGuardError, LLMTimeoutError, LLMResponseError
    """
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

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise EnvironmentError("ANTHROPIC_API_KEY not set")

    client = anthropic.Anthropic(api_key=api_key)

    t0 = time.monotonic()
    raw = _call_api(client, prompt)
    elapsed = time.monotonic() - t0

    log.info(
        "llm_client response app_id=%s elapsed_ms=%d",
        app_data.get("id", "unknown"),
        int(elapsed * 1000),
    )

    try:
        data = json.loads(raw)
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
