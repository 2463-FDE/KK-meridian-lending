"""Unit coverage for payment-service's own copy of the redactor.

`redactor.py` is a copy of `loan-assistant/app/redactor.py`, and it is tested
there -- but the copy is what runs here, and this PR modifies it (adding
`processor_token` to the sensitive-key set). The integration side is already
covered: test_charge_flow.py asserts the real `charge()` log line carries no
processor token. What was missing is the rule set itself.

That rule set is not dead code in this service. `charge()` passes `name` and
the caller-supplied `idempotency_key` through `redact_dict`, and both are free
text -- so the PAN/SSN/CVV string rules can fire on real input, and a
false-positive would corrupt a log line an operator relies on while a
false-negative would leak the thing the whole ADR-0008 change exists to stop.

Card numbers here are the standard published test values (Visa/Mastercard/Amex
test PANs), never real cards.
"""
import pytest

from app.redactor import redact_dict, redact_str

# Published test card numbers -- Luhn-valid by construction, issued to nobody.
VISA = "4111111111111111"
MASTERCARD = "5555555555554444"
AMEX = "378282246310005"


# --- sensitive keys -----------------------------------------------------------

@pytest.mark.parametrize("key", [
    "pan", "cvv", "ssn", "card_number", "card_no", "social_security_number",
    "processor_token",
])
def test_a_sensitive_key_is_replaced_whatever_its_value(key):
    out = redact_dict({key: "anything-at-all"})
    assert out[key] == "[REDACTED]"


def test_processor_token_is_redacted_by_key_not_by_pattern():
    """The addition this PR makes. A vaulted token is opaque -- it matches no
    PAN/SSN/CVV pattern -- so key-based redaction is the only thing that catches
    it, and it is still sensitive because it can be replayed against the
    processor."""
    # Deliberately low-entropy and obviously fake. An earlier version of this
    # test used a realistic `tok_live_<random>` literal and the repository's
    # gitleaks job flagged it as a leaked key -- correctly, since it cannot tell
    # a plausible token from a real one. What this test needs is an OPAQUE value
    # (one matching no PAN/SSN/CVV pattern), not a realistic one.
    out = redact_dict({"processor_token": "tok_test_placeholder_value"})
    assert out["processor_token"] == "[REDACTED]"
    assert "placeholder_value" not in str(out)


@pytest.mark.parametrize("key", ["PAN", "Cvv", "Processor_Token", "SSN"])
def test_sensitive_key_matching_is_case_insensitive(key):
    assert redact_dict({key: "x"})[key] == "[REDACTED]"


def test_redact_dict_does_not_mutate_its_input():
    """The caller keeps using the original -- `charge()` builds the log dict
    from live values. Mutating in place would redact the real request."""
    original = {"processor_token": "tok_abc", "loan_id": 7}
    out = redact_dict(original)
    assert original["processor_token"] == "tok_abc"
    assert out["processor_token"] == "[REDACTED]"


def test_nested_dicts_and_lists_are_reached():
    out = redact_dict({
        "outer": {"pan": VISA, "items": [{"cvv": "123"}, f"card {VISA}"]},
    })
    assert out["outer"]["pan"] == "[REDACTED]"
    assert out["outer"]["items"][0]["cvv"] == "[REDACTED]"
    assert VISA not in str(out)


# --- the string rules, which are live for `name` and `idempotency_key` --------

@pytest.mark.parametrize("pan", [VISA, MASTERCARD, AMEX])
def test_a_luhn_valid_card_number_in_free_text_is_masked(pan):
    out = redact_str(f"charge attempt for {pan} declined")
    assert pan not in out
    assert "[PAN-REDACTED]" in out


@pytest.mark.parametrize("spaced", ["4111 1111 1111 1111", "4111-1111-1111-1111"])
def test_a_separated_card_number_is_masked_too(spaced):
    assert "[PAN-REDACTED]" in redact_str(f"card {spaced} on file")


def test_a_digit_run_that_fails_luhn_is_left_alone():
    """Deliberate: masking every 16-digit run would eat order ids and
    authorization ids, and an operator needs those to trace a payment."""
    not_a_card = "4111111111111112"   # last digit broken
    assert not_a_card in redact_str(f"authorization {not_a_card}")


def test_last4_and_brand_survive_redaction():
    """These two exist precisely so a masked payment is still identifiable. If
    redaction ate them the log line would be useless."""
    out = redact_dict({"last4": "4242", "brand": "visa"})
    assert out["last4"] == "4242"
    assert out["brand"] == "visa"


@pytest.mark.parametrize("ssn", ["123-45-6789", "123 45 6789", "123456789"])
def test_ssn_forms_are_masked(ssn):
    out = redact_str(f"applicant {ssn} verified")
    assert ssn not in out
    assert "[SSN-REDACTED]" in out


@pytest.mark.parametrize("phrasing", [
    "cvv 123", "CVC: 456", "security code 789", "card verification value 1234",
])
def test_cvv_is_masked_across_phrasings(phrasing):
    out = redact_str(f"caller sent {phrasing} in the clear")
    assert "[CVV-REDACTED]" in out


def test_a_caller_supplied_idempotency_key_that_looks_like_a_card_is_masked():
    """Fail closed. `idempotency_key` is caller-supplied free text and goes
    through this on every charge -- if one happens to be a Luhn-valid digit run,
    masking it costs a traceable log line, which is the cheaper mistake."""
    out = redact_dict({"idempotency_key": VISA})
    assert VISA not in str(out)


def test_a_name_carrying_an_ssn_is_masked():
    """`charge()` passes the borrower name straight through. Whatever ends up in
    that field must not reach the log unredacted."""
    out = redact_dict({"name": "Robin Fictional 123-45-6789"})
    assert "123-45-6789" not in out["name"]
    assert "Robin Fictional" in out["name"]
