"""
Production LLM client for loan application summarization.

Guardrails built in:
  1. PII/PCI redaction  — SSN, PAN, CVV stripped before prompt is built
  2. Timeout           — 20 s hard wall; raises LLMTimeoutError
  3. Retry             — 3 attempts, exponential backoff, on 5xx / network errors only
  4. Cost guard        — refuses input token estimate above MAX_INPUT_TOKENS
  5. Structured output — returns validated LoanSummary (Pydantic); never raw text
  6. Redacted logging  — logs show redacted payload; raw values never appear

Never bypass the redactor before calling summarize_application().
Never increase MAX_INPUT_TOKENS without a cost-review sign-off.
"""

import json
import logging
import os
import time
from typing import Any

import anthropic
import httpx
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

_SYSTEM = """
You are a loan officer assistant at Meridian Lending. Given a loan application,
produce a concise, factual summary in the exact JSON schema provided.

Rules:
- NEVER include SSN, full card numbers, or CVV in your output.
- Use only the data provided — do not invent information.
- risk_tier: 'low' if DTI<30% and employment>2yr; 'high' if DTI>50% or employment<6mo;
  'decline' if income cannot plausibly service the loan; else 'medium'.
- flags: list specific concerns (e.g. "Employment < 1 year", "Loan-to-income ratio > 4x").
  Empty list if none.
""".strip()


class LLMTimeoutError(Exception):
    pass


class LLMCostGuardError(Exception):
    pass


class LLMResponseError(Exception):
    pass


def _build_prompt(app_data: dict) -> str:
    safe = redact_dict(app_data)
    return (
        "Summarize this loan application as JSON matching the LoanSummary schema.\n\n"
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
        summary = LoanSummary(**data)
    except Exception as exc:
        safe_raw = redact_str(raw)
        log.error("llm_client parse error response=%s", safe_raw)
        raise LLMResponseError(f"Could not parse LLM response: {exc}") from exc

    return summary
