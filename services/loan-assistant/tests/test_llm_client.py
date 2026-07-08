"""Tests for the prompt field allowlist and applicant-name handling.

Regression for the Codex review on PR #2: _build_prompt() previously serialized the
full application record after redact_dict(), which only masks SSN/PAN/CVV-shaped
values -- applicant name, email, phone, and address passed straight through to the
Anthropic prompt untouched. Now only non-identifying underwriting fields are ever
included, and applicant_name in the final response comes from trusted server-side
data, never from anything the model returns.
"""
import json

from app.llm_client import _build_prompt, summarize_application

APP_DATA = {
    "id": 42,
    "amount": 15000,
    "term_months": 36,
    "purpose": "debt_consolidation",
    "employer": "Acme Corp",
    "job_title": "Engineer",
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
