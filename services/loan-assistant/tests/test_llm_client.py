"""Tests for the prompt field allowlist and applicant-name handling.

Regression for the Codex review on PR #2: _build_prompt() previously serialized the
full application record after redact_dict(), which only masks SSN/PAN/CVV-shaped
values -- applicant name, email, phone, and address passed straight through to the
Anthropic prompt untouched. Now only non-identifying underwriting fields are ever
included, and applicant_name in the final response comes from trusted server-side
data, never from anything the model returns.
"""
import json

import anthropic
import pytest

from app import config, llm_client
from app.llm_client import (
    LLMConfigError,
    LLMInsufficientDataError,
    _build_prompt,
    _make_client,
    _model_id,
    summarize_application,
)

APP_DATA = {
    "id": 42,
    "amount": 15000,
    "term_months": 36,
    "purpose": "debt_consolidation",
    "income": 85000,
    "employer": "Acme Corp",
    "job_title": "Engineer",
    "employment_years": 4.5,
    "applicant": {
        "id": 1,
        "name": "Maria Gonzalez",
        "email": "maria.gonzalez@example.com",
        "phone": "555-0100",
        "address": "118 Larkspur Ave, Fresno, CA 93722",
        "is_entity": False,
    },
}


def test_build_prompt_excludes_contact_identifiers():
    prompt = _build_prompt(APP_DATA)
    assert "Maria Gonzalez" not in prompt
    assert "maria.gonzalez@example.com" not in prompt
    assert "555-0100" not in prompt
    assert "Larkspur" not in prompt


def test_build_prompt_includes_underwriting_fields():
    prompt = _build_prompt(APP_DATA)
    assert "15000" in prompt
    assert "debt_consolidation" in prompt
    assert "Acme Corp" in prompt


def test_build_prompt_includes_risk_grounding_data():
    """Regression (Codex review on PR #2): the system prompt tells the model to
    judge risk_tier from DTI and employment length, so income and employment_years
    must actually reach the prompt -- otherwise the model was inventing a risk chip
    from data it never received."""
    prompt = _build_prompt(APP_DATA)
    assert "85000" in prompt
    assert "4.5" in prompt


def test_summarize_application_refuses_without_income(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    incomplete = {**APP_DATA, "income": None}
    with pytest.raises(LLMInsufficientDataError):
        summarize_application(incomplete)


def test_summarize_application_refuses_without_employment_years(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    incomplete = {**APP_DATA, "employment_years": None}
    with pytest.raises(LLMInsufficientDataError):
        summarize_application(incomplete)


def test_summarize_application_fills_applicant_name_from_trusted_data(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")

    llm_json = json.dumps(
        {
            "loan_amount": 15000,
            "term_months": 36,
            "purpose": "debt_consolidation",
            "risk_tier": "low",
            "summary": "Stable income, short-term consolidation loan.",
            "flags": [],
        }
    )
    monkeypatch.setattr("app.llm_client._call_api", lambda client, prompt: llm_json)

    result = summarize_application(APP_DATA)

    assert result.applicant_name == "Maria Gonzalez"
    assert result.risk_tier == "low"


def test_summarize_application_ignores_any_name_the_model_tries_to_return(monkeypatch):
    """Even if the model hallucinated a name (it shouldn't, since it was never
    given one), the response uses the trusted server-side name, not the model's."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")

    llm_json = json.dumps(
        {
            "applicant_name": "Someone Else Entirely",
            "loan_amount": 15000,
            "term_months": 36,
            "purpose": "debt_consolidation",
            "risk_tier": "low",
            "summary": "Stable income, short-term consolidation loan.",
            "flags": [],
        }
    )
    monkeypatch.setattr("app.llm_client._call_api", lambda client, prompt: llm_json)

    result = summarize_application(APP_DATA)

    assert result.applicant_name == "Maria Gonzalez"


# --- LLM_PROVIDER: config-driven switch between the direct Anthropic API and
# AWS Bedrock (anthropic.AnthropicBedrock). Client construction is a pure config
# object in both cases -- no network call -- so these need no live API/AWS access,
# same reasoning the tests above already rely on for a fake ANTHROPIC_API_KEY.

def test_make_client_defaults_to_anthropic(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    client = _make_client()
    assert isinstance(client, anthropic.Anthropic)
    assert not isinstance(client, anthropic.AnthropicBedrock)


def test_make_client_uses_bedrock_when_configured(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "bedrock")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "test-bedrock-token-not-real")
    client = _make_client()
    assert isinstance(client, anthropic.AnthropicBedrock)


# --- LangSmith tracing: wrap_anthropic() patches the client in place and
# returns the same object (confirmed against the installed SDK), and its
# patched call is a no-op unless LANGSMITH_TRACING/LANGSMITH_API_KEY are set --
# safe to always attempt. Must fail open: tracing is observability, not a
# compliance guardrail, so a broken wrap must never block a real call.

def test_make_client_wraps_for_tracing_when_available(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")

    calls = []

    def _spy_wrap(client):
        calls.append(client)
        return client

    monkeypatch.setattr(llm_client, "wrap_anthropic", _spy_wrap)
    client = _make_client()

    assert len(calls) == 1
    assert calls[0] is client
    assert isinstance(client, anthropic.Anthropic)


def test_make_client_falls_back_when_wrapping_fails(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")

    def _broken_wrap(client):
        raise RuntimeError("simulated LangSmith misconfiguration")

    monkeypatch.setattr(llm_client, "wrap_anthropic", _broken_wrap)

    # Must not raise -- a broken trace wrapper must never block a real call.
    client = _make_client()
    assert isinstance(client, anthropic.Anthropic)


def test_model_id_defaults_to_direct_api_model(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "anthropic")
    assert _model_id() == llm_client.MODEL


def test_model_id_requires_bedrock_model_id_when_using_bedrock(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "bedrock")
    monkeypatch.setattr(config, "BEDROCK_MODEL_ID", "")
    with pytest.raises(LLMConfigError):
        _model_id()


def test_model_id_uses_configured_bedrock_model_id(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "bedrock")
    monkeypatch.setattr(config, "BEDROCK_MODEL_ID", "anthropic.claude-sonnet-test-v1:0")
    assert _model_id() == "anthropic.claude-sonnet-test-v1:0"


# --- Regression: a real Bedrock Claude response (live-verified) wrapped its JSON
# in a markdown code fence despite the prompt saying not to. json.loads() choked
# on the fence. Strip it defensively rather than relying solely on prompt
# compliance -- providers/models don't always follow that instruction.

def test_summarize_application_strips_markdown_json_fence(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")

    fenced = (
        "```json\n"
        + json.dumps(
            {
                "loan_amount": 15000,
                "term_months": 36,
                "purpose": "debt_consolidation",
                "risk_tier": "low",
                "summary": "Stable income, short-term consolidation loan.",
                "flags": [],
            }
        )
        + "\n```"
    )
    monkeypatch.setattr("app.llm_client._call_api", lambda client, prompt: fenced)

    result = summarize_application(APP_DATA)

    assert result.risk_tier == "low"
    assert result.applicant_name == "Maria Gonzalez"


def test_strip_markdown_fences_leaves_plain_json_unchanged():
    plain = '{"a": 1}'
    assert llm_client._strip_markdown_fences(plain) == plain


def test_strip_markdown_fences_strips_json_fence():
    fenced = '```json\n{"a": 1}\n```'
    assert llm_client._strip_markdown_fences(fenced) == '{"a": 1}'


def test_strip_markdown_fences_strips_bare_fence():
    fenced = '```\n{"a": 1}\n```'
    assert llm_client._strip_markdown_fences(fenced) == '{"a": 1}'
