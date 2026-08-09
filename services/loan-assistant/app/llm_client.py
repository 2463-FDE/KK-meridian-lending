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

from . import config, macro
from .redactor import redact_dict, redact_str
from .schemas import ExternalSignal, LoanSummary

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


def _build_prompt(app_data: dict, signal=None) -> str:
    allowed = {k: app_data.get(k) for k in _PROMPT_ALLOWED_FIELDS if app_data.get(k) is not None}
    safe = redact_dict(allowed)
    # The signal is context, not an input the model may invent. Appended as a
    # labelled block rather than merged into the application data, so the model
    # cannot mistake a published statistic for something the applicant stated.
    # The value the officer finally sees is attached server-side from the same
    # fetch (see summarize_application), not read back out of here.
    context = ""
    if signal is not None:
        context = (
            "External context (published statistic, NOT supplied by the applicant):"
            + chr(10) + signal.cite() + chr(10)
            + "You may reference this when it is relevant to employment or repayment "
            "risk. Do not restate it as a fact about this applicant, and do not "
            "invent any other external figures." + chr(10) + chr(10)
        )
    return (
        "Summarize this loan application as JSON with fields: loan_amount, "
        "term_months, purpose, risk_tier, summary, flags.\n\n"
        f"Application data:\n{json.dumps(safe, indent=2)}\n\n"
        f"{context}"
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


# Words that mark a sentence as being ABOUT the external signal. Derived from
# the signal's own label rather than hardcoded, so adding a series to
# macro._SERIES_METADATA does not silently leave its prose unguarded. The
# generic parts of a label carry no topic, so they are dropped.
_LABEL_STOPWORDS = frozenset({
    "us", "u.s.", "the", "and", "rate", "index", "seasonally", "adjusted",
    "national", "total", "all", "persons", "of",
})
# A percentage or bare decimal, e.g. "11.9%", "11.9 percent", "4.2".
_FIGURE_RE = re.compile(r"\d+(?:\.\d+)?")
# The same figure, but only where it is written AS the signal's unit -- "4.2%"
# or "4.2 percent". Used to tell a claim about the signal apart from an
# unrelated number in the same sentence (an income, a loan amount).
_UNIT_FIGURE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*(?:%|percent|pct)", re.IGNORECASE)
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")
# Abbreviations whose full stop does not end a sentence. Without this the
# splitter cut "The U.S. unemployment rate is 11.9%." into "The U.S." plus the
# rest -- removing the false claim left the orphan "The U.S." in the summary,
# which passed the nonempty check and put malformed prose in front of an
# officer. Reviewed on PR #13.
_ABBREVIATIONS = (
    "U.S.", "U.K.", "E.U.", "e.g.", "i.e.", "etc.", "approx.", "vs.", "No.",
    "Inc.", "Ltd.", "Co.", "Mr.", "Mrs.", "Ms.", "Dr.", "Jr.", "Sr.", "St.",
)
_ABBREV_SENTINEL = "<<ABBREV>>"


def _split_sentences(text: str) -> list:
    """Sentence split that does not treat an abbreviation's dot as a full stop."""
    masked = text
    for i, abbrev in enumerate(_ABBREVIATIONS):
        masked = masked.replace(abbrev, abbrev.replace(".", f"{_ABBREV_SENTINEL}{i}{_ABBREV_SENTINEL}"))
    parts = _SENTENCE_SPLIT_RE.split(masked)
    restored = []
    for part in parts:
        for i, abbrev in enumerate(_ABBREVIATIONS):
            part = part.replace(f"{_ABBREV_SENTINEL}{i}{_ABBREV_SENTINEL}", ".")
        restored.append(part)
    return restored


def _signal_topic_words(signal) -> set[str]:
    words = {
        w.strip("(),.").lower()
        for w in f"{signal.label} {signal.source}".split()
    }
    words |= {"bls", signal.series_id.lower()}
    return {w for w in words if len(w) > 2 and w not in _LABEL_STOPWORDS}


def _drops_a_contradicting_claim(text: str, signal) -> bool:
    """Whether `text` states a figure for the signal that is not the published one.

    Narrow on purpose. It only fires on a sentence that both NAMES the signal's
    subject and carries a number that is not the published value -- so "the
    labour market is weakening" survives, and so does an accurate restatement.
    """
    topic = _signal_topic_words(signal)
    lowered = text.lower()
    if not any(word in lowered for word in topic):
        return False

    # Figures written AS the signal's unit are the claims about it. Preferring
    # them keeps an unrelated number in the same sentence -- an income, a loan
    # amount -- from being read as a rate. If the sentence carries none, every
    # figure in it is a candidate, which is the conservative reading.
    published = float(signal.value)
    unit_figures = [float(m) for m in _UNIT_FIGURE_RE.findall(text)]
    claims = unit_figures or [float(m) for m in _FIGURE_RE.findall(text)]
    if not claims:
        return False

    # EVERY claim must be the published one. The previous version accepted the
    # whole sentence as soon as ONE number matched, so "Unemployment is 11.9%,
    # while the cited series value is 4.2%" survived intact and was rendered
    # beside the 4.2% citation -- the reviewed defect, in a sentence that
    # contradicts itself as well as the source. Reviewed on PR #13.
    return any(abs(f - published) >= 0.05 for f in claims)


def _strip_contradicting_macro_claims(summary: str, flags: list, signal):
    """Remove model prose that gives the external figure a different value.

    The prompt asks the model not to repeat the number at all. This is what
    happens when it does so anyway and gets it wrong -- which a prompt cannot
    prevent, only discourage. Reviewed on PR #13: the officer was shown the
    model's "unemployment is 11.9%" directly above the provider's cited 4.2%,
    and the whole point of a grounded citation is that it is not contradicted
    on the same screen.

    Removal rather than correction: rewriting a number inside a sentence would
    make the service the author of a claim it cannot stand behind, and the
    published figure is already displayed in full beside the summary.
    """
    if signal is None:
        return summary, flags, 0

    dropped = 0
    kept_sentences = []
    for sentence in _split_sentences(summary):
        if sentence and _drops_a_contradicting_claim(sentence, signal):
            dropped += 1
            continue
        kept_sentences.append(sentence)
    kept_flags = []
    for flag in flags:
        if isinstance(flag, str) and _drops_a_contradicting_claim(flag, signal):
            dropped += 1
            continue
        kept_flags.append(flag)

    cleaned = " ".join(s for s in kept_sentences if s).strip()
    if dropped:
        log.warning(
            "removed %d model claim(s) contradicting the published %s "
            "(published=%s%s, period=%s)",
            dropped, signal.label, signal.value, signal.unit, signal.period,
        )
    if not cleaned:
        # Everything the model wrote was a false statement about the signal.
        # There is no summary left to show, and inventing one here would make
        # this service the author. Fail closed, like every other guardrail.
        raise LLMResponseError(
            "the model's summary consisted only of claims contradicting the "
            "published external figure"
        )
    return cleaned, kept_flags, dropped


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

    # One grounded external signal (app/macro.py). Fetched before the cost
    # guard, never after: it adds tokens, so the guard has to see the prompt
    # that will actually be sent. Fails open -- None simply means no context.
    signal = macro.current_signal()

    # The BASE prompt is measured first, and it is the one the guard judges. The
    # signal is optional context that fails open everywhere else; letting its
    # tokens push an otherwise-valid application over the ceiling would make an
    # optional extra the reason a summary 400s -- and it is reachable, because
    # `purpose`, `employer` and `job_title` carry no maximum length in
    # origination's ApplicationIn. Reviewed on PR #13.
    base_prompt = _build_prompt(app_data, None)
    base_estimated = _estimate_tokens(_SYSTEM + base_prompt)
    if base_estimated > MAX_INPUT_TOKENS:
        raise LLMCostGuardError(
            f"Estimated input tokens ({base_estimated}) exceeds guard ({MAX_INPUT_TOKENS}). "
            "Trim the application payload before calling summarize_application()."
        )

    prompt = _build_prompt(app_data, signal)
    estimated = _estimate_tokens(_SYSTEM + prompt)
    # The review asked for the signal's cost to be measured, not assumed, so the
    # delta is computed here and logged on every call.
    signal_tokens = estimated - base_estimated if signal is not None else 0

    if signal is not None and estimated > MAX_INPUT_TOKENS:
        # Only the citation crosses the line. Drop it and answer without the
        # external context, which is exactly what happens when the provider is
        # disabled or unreachable -- the summary is still valid, it simply has
        # nothing external to show.
        log.warning(
            "omitting the external signal to stay inside the cost guard "
            "app_id=%s base_tokens=%d with_signal=%d guard=%d",
            app_data.get("id", "unknown"), base_estimated, estimated, MAX_INPUT_TOKENS,
        )
        signal = None
        prompt = base_prompt
        estimated = base_estimated
        signal_tokens = 0

    log.info(
        "llm_client summarize app_id=%s estimated_tokens=%d signal=%s signal_tokens=%d",
        app_data.get("id", "unknown"),
        estimated,
        signal.series_id if signal else "none",
        signal_tokens,
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

    # The citation is built from what the provider returned, not from anything
    # the model produced -- the same rule _applicant_name follows. If the model
    # restated the rate differently in its prose, the cited figure is still the
    # published one.
    signals = []
    if signal is not None:
        signals.append(
            ExternalSignal(
                source=signal.source, series_id=signal.series_id, label=signal.label,
                value=signal.value, unit=signal.unit, period=signal.period,
                url=signal.url, citation=signal.cite(),
            )
        )

    # The prose must not contradict the figure printed beside it. The prompt
    # asks the model not to repeat the number at all; this is the half that
    # holds when it does anyway and gets it wrong.
    fields = llm_output.model_dump()
    fields["summary"], fields["flags"], _ = _strip_contradicting_macro_claims(
        fields["summary"], fields.get("flags") or [], signal,
    )

    return LoanSummary(
        applicant_name=_applicant_name(app_data),
        external_signals=signals,
        **fields,
    )


def _applicant_name(app_data: dict) -> str:
    """Applicant name for the response, taken from trusted server-side data — the
    LLM is never given it, so it can't be the source of this field."""
    applicant = app_data.get("applicant")
    if isinstance(applicant, dict):
        return applicant.get("name") or "Applicant"
    if isinstance(applicant, str) and applicant:
        return applicant
    return app_data.get("applicant_name") or "Applicant"
