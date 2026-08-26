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
    make_client,
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
    ground its prose in income and employment length, so income and employment_years
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


def test_insufficient_data_error_names_only_the_actually_missing_field(monkeypatch):
    """Bug fix: this used to always name BOTH risk-grounding fields in the
    error regardless of which one was actually missing -- income=85000 right
    there in APP_DATA, but the message still claimed 'income' was missing
    too. A staff reviewer reading that would reasonably believe income data
    was lost, not just employment_years."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    incomplete = {**APP_DATA, "employment_years": None}

    with pytest.raises(LLMInsufficientDataError) as exc_info:
        summarize_application(incomplete)

    message = str(exc_info.value)
    assert "employment_years" in message
    assert "income" not in message


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
    monkeypatch.setattr("app.llm_client._summary_text_via_agent",
                        lambda prompt: llm_json)

    result = summarize_application(APP_DATA)

    assert result.applicant_name == "Maria Gonzalez"
    assert not hasattr(result, "risk_tier"), (
        "risk_tier reached the response model. It forced the model to invent a "
        "classification boundary with no published rule behind it, and staff saw "
        "the result as a policy-looking chip."
    )


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
    monkeypatch.setattr("app.llm_client._summary_text_via_agent",
                        lambda prompt: llm_json)

    result = summarize_application(APP_DATA)

    assert result.applicant_name == "Maria Gonzalez"


# --- LLM_PROVIDER: config-driven switch between the direct Anthropic API and
# AWS Bedrock (anthropic.AnthropicBedrock). Client construction is a pure config
# object in both cases -- no network call -- so these need no live API/AWS access,
# same reasoning the tests above already rely on for a fake ANTHROPIC_API_KEY.

def test_make_client_defaults_to_anthropic(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    client = make_client()
    assert isinstance(client, anthropic.Anthropic)
    assert not isinstance(client, anthropic.AnthropicBedrock)


def test_make_client_uses_bedrock_when_configured(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "bedrock")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "test-bedrock-token-not-real")
    client = make_client()
    assert isinstance(client, anthropic.AnthropicBedrock)


# --- LangSmith tracing: wrap_anthropic() patches the client in place and
# returns the same object (confirmed against the installed SDK), and its
# patched call is a no-op unless LANGSMITH_TRACING/LANGSMITH_API_KEY are set --
# safe to always attempt. Must fail open: tracing is observability, not a
# compliance guardrail, so a broken wrap must never block a real call.

def test_make_client_does_not_wrap_for_tracing(monkeypatch):
    """The client is handed back unwrapped, even with tracing switched on.

    `make_client` used to end with `wrap_anthropic(client)`, which patches
    `.messages.create` in place and records the call -- for this client, the
    user's question and the retrieved policy excerpt, both on the prohibited
    list. The client never asked for raw Policy Chat traces.

    Detected structurally rather than by behaviour: `wrap_anthropic` installs an
    instance attribute over the class method, so a wrapped client has `create` in
    `vars(client.messages)` and an unwrapped one does not. Verified against the
    installed SDK in both states, which is what makes this assertion meaningful
    rather than a guess about an implementation detail.

    Tracing is deliberately ON here. With it off the assertion would hold for the
    wrong reason -- the old wrapper was a passthrough when tracing was
    unconfigured, and this test has to fail if the wrapping comes back.
    """
    monkeypatch.setattr(config, "LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_not_real")

    client = make_client()

    assert isinstance(client, anthropic.Anthropic)
    assert "create" not in vars(client.messages), (
        "make_client returned a client whose messages.create has been patched -- "
        "the LangSmith wrapper is back, and with it the prompt and completion")


def test_the_wrapper_is_gone_from_the_module_entirely(monkeypatch):
    """Not merely unused -- absent.

    A module-level `wrap_anthropic` left in place is one edit away from being
    called again, and the edit would look like a one-line restoration of
    observability rather than the re-opening of a content-retention hole.
    """
    assert not hasattr(llm_client, "wrap_anthropic"), (
        "llm_client still holds a reference to wrap_anthropic")


def test_a_bedrock_client_is_not_wrapped_either(monkeypatch):
    """Both provider branches, because the wrapping used to sit after the if.

    A fix applied to one branch and not the other is the shape of this bug
    reappearing on the provider nobody tested.
    """
    monkeypatch.setattr(config, "LLM_PROVIDER", "bedrock")
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_pt_not_real")
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_BEARER_TOKEN_BEDROCK", "not-a-real-token")

    client = make_client()

    assert "create" not in vars(client.messages)


# --- Review finding: an unrecognized LLM_PROVIDER (e.g. a typo like "berock")
# silently fell into the direct-Anthropic branch instead of failing loudly --
# fail closed on anything that isn't an exact, known provider name.

def test_make_client_rejects_unrecognized_provider(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "berock")  # typo for "bedrock"
    with pytest.raises(LLMConfigError):
        make_client()


def test_model_id_rejects_unrecognized_provider(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "berock")
    with pytest.raises(LLMConfigError):
        _model_id()


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
    monkeypatch.setattr("app.llm_client._summary_text_via_agent",
                        lambda prompt: fenced)

    result = summarize_application(APP_DATA)

    assert not hasattr(result, "risk_tier")
    assert result.applicant_name == "Maria Gonzalez"


def test_strip_markdown_fences_leaves_plain_json_unchanged():
    plain = '{"a": 1}'
    assert llm_client.strip_markdown_fences(plain) == plain


def test_strip_markdown_fences_strips_json_fence():
    fenced = '```json\n{"a": 1}\n```'
    assert llm_client.strip_markdown_fences(fenced) == '{"a": 1}'


def test_strip_markdown_fences_strips_bare_fence():
    fenced = '```\n{"a": 1}\n```'
    assert llm_client.strip_markdown_fences(fenced) == '{"a": 1}'


# --- the refusal is read by a loan officer, not by a developer -----------------

def test_the_refusal_detail_is_plain_english():
    """The 422 body is rendered verbatim in the UI.

    It used to be `app_id=7577 is missing ['income', 'employment_years'] —
    refusing to let the model describe risk from data it never saw`: a Python
    list literal and an `app_id=` prefix, in front of a loan officer. That reads
    as a crash rather than a decision, which undercuts the trust the refusal
    itself earns. Reported by a reviewer who hit it in the running app.
    """
    from app.llm_client import LLMInsufficientDataError, summarize_application

    try:
        summarize_application({"id": 7577, "amount": 500, "term_months": 12})
    except LLMInsufficientDataError as exc:
        assert "app_id=" not in exc.detail, "the reader is shown an internal identifier"
        assert "[" not in exc.detail, "the reader is shown a Python list literal"
        assert "income" in exc.detail and "employment history" in exc.detail
        assert "guesswork" in exc.detail
    else:
        raise AssertionError("no refusal was raised")


def test_the_log_message_still_names_the_row_and_the_fields():
    """Guard the guard. Making the detail readable must not cost the developer
    the identifier and the field names -- the two strings are different jobs,
    not a translation of one another."""
    from app.llm_client import LLMInsufficientDataError, summarize_application

    try:
        summarize_application({"id": 7577, "amount": 500, "term_months": 12})
    except LLMInsufficientDataError as exc:
        assert "app_id=7577" in str(exc)
        assert "income" in str(exc) and "employment_years" in str(exc)


def test_only_the_absent_field_is_named_to_the_reader():
    """A summary refused for a missing employment history must not tell the
    officer their income data is gone -- income is right there."""
    from app.llm_client import LLMInsufficientDataError, summarize_application

    try:
        summarize_application({"id": 5678, "amount": 9000, "term_months": 24,
                               "income": 60000})
    except LLMInsufficientDataError as exc:
        assert "employment history" in exc.detail
        assert "no recorded income" not in exc.detail
