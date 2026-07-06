"""
Regression test for the free-text PII leak found in code review:
_redact_node() used to only mask values under known-sensitive keys, so an SSN/PAN
sitting inside an ordinary field (notes, comments, OCR text) reached the LLM prompt
untouched. redact_dict() must now redact every string leaf, not just sensitive keys.
"""
from app.redactor import redact_dict
from app.llm_client import _build_prompt

SSN = "123-45-6789"
PAN = "4111 1111 1111 1111"


def test_redact_dict_masks_free_text_field():
    safe = redact_dict({"notes": f"SSN {SSN} card {PAN}"})
    assert SSN not in safe["notes"]
    assert PAN not in safe["notes"]


def test_redact_dict_masks_free_text_in_nested_dict():
    safe = redact_dict({"applicant": {"comments": f"borrower gave SSN {SSN}"}})
    assert SSN not in safe["applicant"]["comments"]


def test_redact_dict_masks_free_text_in_list():
    safe = redact_dict({"ocr_lines": [f"card on file: {PAN}", "no PII here"]})
    assert PAN not in safe["ocr_lines"][0]
    assert safe["ocr_lines"][1] == "no PII here"


def test_redact_dict_still_masks_known_sensitive_keys():
    safe = redact_dict({"ssn": SSN, "pan": PAN, "cvv": "123"})
    assert safe["ssn"] == "[REDACTED]"
    assert safe["pan"] == "[REDACTED]"
    assert safe["cvv"] == "[REDACTED]"


def test_build_prompt_never_includes_ssn_or_pan_from_free_text():
    app_data = {
        "id": 1,
        "notes": f"applicant mentioned SSN {SSN} and card {PAN}",
        "applicant": {"comments": f"also gave {SSN} verbally"},
        "ocr_lines": [f"scanned check: {PAN}"],
    }
    prompt = _build_prompt(app_data)
    assert SSN not in prompt
    assert PAN not in prompt
