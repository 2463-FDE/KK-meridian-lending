"""Request-validation tests for the application schema (these PASS)."""
import pytest
from pydantic import ValidationError

from app.schemas import ApplicationIn


def test_valid_application():
    a = ApplicationIn(name="Test Borrower", amount=10000, term_months=36)
    assert a.amount == 10000
    assert a.term_months == 36


def test_amount_over_cap_rejected():
    with pytest.raises(ValidationError):
        ApplicationIn(name="Test", amount=75000, term_months=36)


def test_term_out_of_range_rejected():
    with pytest.raises(ValidationError):
        ApplicationIn(name="Test", amount=10000, term_months=6)


def test_name_required():
    with pytest.raises(ValidationError):
        ApplicationIn(name="", amount=10000)


def test_phone_non_digits_rejected():
    with pytest.raises(ValidationError):
        ApplicationIn(name="Test", amount=10000, phone="not-a-phone")


def test_phone_wrong_length_rejected():
    with pytest.raises(ValidationError):
        ApplicationIn(name="Test", amount=10000, phone="555123")


def test_phone_formatted_accepted_and_normalized():
    a = ApplicationIn(name="Test", amount=10000, phone="(555) 123-4567")
    assert a.phone == "5551234567"


def test_phone_with_country_code_normalized():
    a = ApplicationIn(name="Test", amount=10000, phone="+1 555-123-4567")
    assert a.phone == "5551234567"


def test_phone_omitted_allowed():
    a = ApplicationIn(name="Test", amount=10000)
    assert a.phone is None


def test_ssn_wrong_length_rejected():
    with pytest.raises(ValidationError):
        ApplicationIn(name="Test", amount=10000, ssn="123456789012")


def test_ssn_non_digits_rejected():
    with pytest.raises(ValidationError):
        ApplicationIn(name="Test", amount=10000, ssn="not-an-ssn")


def test_ssn_formatted_accepted_and_normalized():
    a = ApplicationIn(name="Test", amount=10000, ssn="123-45-6789")
    assert a.ssn == "123456789"


def test_ssn_omitted_allowed():
    a = ApplicationIn(name="Test", amount=10000)
    assert a.ssn is None


def test_zip_wrong_length_rejected():
    with pytest.raises(ValidationError):
        ApplicationIn(name="Test", amount=10000, zip_code="123")


def test_zip_non_digits_rejected():
    with pytest.raises(ValidationError):
        ApplicationIn(name="Test", amount=10000, zip_code="not-a-zip")


def test_zip_plus4_normalized_to_base_five():
    a = ApplicationIn(name="Test", amount=10000, zip_code="20912-1234")
    assert a.zip_code == "20912"


def test_zip_five_digit_accepted():
    a = ApplicationIn(name="Test", amount=10000, zip_code="20912")
    assert a.zip_code == "20912"


def test_zip_omitted_allowed():
    a = ApplicationIn(name="Test", amount=10000)
    assert a.zip_code is None
