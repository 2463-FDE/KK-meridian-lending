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


# Regression: reviewer (Codex) found these leaking through untouched — only the
# hyphenated SSN and JSON-style cvv:/cvv=/"cvv": forms were masked. A free-text note
# or OCR field using any of these common variants reached the LLM prompt raw.
SSN_UNFORMATTED = "123456789"
SSN_SPACE_SEPARATED = "123 45 6789"
CVV_VALUE = "123"


def test_build_prompt_never_includes_unformatted_ssn():
    app_data = {"notes": f"applicant SSN {SSN_UNFORMATTED} on file"}
    prompt = _build_prompt(app_data)
    assert SSN_UNFORMATTED not in prompt


def test_build_prompt_never_includes_space_separated_ssn():
    app_data = {"notes": f"applicant SSN {SSN_SPACE_SEPARATED} on file"}
    prompt = _build_prompt(app_data)
    assert SSN_SPACE_SEPARATED not in prompt


def test_build_prompt_never_includes_prose_cvv():
    app_data = {
        "notes_short": f"cvv {CVV_VALUE} confirmed",
        "notes_sentence": f"Borrower gave the CVV is {CVV_VALUE} over the phone.",
    }
    prompt = _build_prompt(app_data)
    assert CVV_VALUE not in prompt


def test_redact_str_masks_unformatted_and_space_separated_ssn():
    from app.redactor import redact_str

    assert SSN_UNFORMATTED not in redact_str(f"SSN {SSN_UNFORMATTED}")
    assert SSN_SPACE_SEPARATED not in redact_str(f"SSN {SSN_SPACE_SEPARATED}")


def test_redact_str_masks_prose_cvv():
    from app.redactor import redact_str

    assert CVV_VALUE not in redact_str(f"cvv {CVV_VALUE}")
    assert CVV_VALUE not in redact_str(f"CVV is {CVV_VALUE}")


# Regression: reviewer (Codex) found a 15-digit Amex-style PAN and a longer-form CVV
# prose phrase leaking through untouched — _PAN_RE only matched exactly 16 digits,
# and _CVV_RE's label was too narrow (only "cvv"/"security code" within 10 chars).
AMEX_PAN = "340000000000009"  # 15 digits, real Amex test number, Luhn-valid
LONG_CVV_PHRASE = "the security code provided as 9876"
CVV_9876 = "9876"


def test_build_prompt_never_includes_15_digit_amex_pan():
    app_data = {"notes": f"card on file: {AMEX_PAN}"}
    prompt = _build_prompt(app_data)
    assert AMEX_PAN not in prompt


def test_build_prompt_never_includes_cvv_in_longer_prose():
    app_data = {"notes": f"Borrower gave {LONG_CVV_PHRASE} over the phone."}
    prompt = _build_prompt(app_data)
    assert CVV_9876 not in prompt


def test_redact_str_masks_amex_pan_and_longer_cvv_prose():
    from app.redactor import redact_str

    assert AMEX_PAN not in redact_str(f"card {AMEX_PAN}")
    assert CVV_9876 not in redact_str(LONG_CVV_PHRASE)


def test_redact_str_leaves_non_card_long_numbers_alone():
    """PAN candidates that fail Luhn (not actually card-shaped) aren't mangled."""
    from app.redactor import redact_str

    not_a_card = "1234567890123"  # 13 digits, fails Luhn
    assert redact_str(f"account {not_a_card}") == f"account {not_a_card}"
