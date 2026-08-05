"""PR #6 review, Gap F -- an incomplete offer is not a disclosure.

Every one of the five canonical TILA amounts used to be read as
`offer.<field> or <default>`:

    rows      = schedule.amortization(principal, offer.apr or 7.99, term_months)
    disclosure = Disclosure(apr=offer.apr or 0, finance_charge=offer.finance_charge or 0, ...)

So a corrupt or half-written offers row was rendered as a real, plausible-looking
Truth-in-Lending disclosure: a NULL APR silently became 7.99%, a NULL finance
charge silently became $0.00. Nothing distinguished those invented numbers from
genuine ones, and the borrower could accept them.

The read path now refuses the row with an explicit integrity error instead.
"""
import pytest
from fastapi.testclient import TestClient

from app import models
from app.database import get_session
from app.main import app

client = TestClient(app)

_COMPLETE = {
    "apr": 5.946, "finance_charge": 768.11, "monthly_payment": 407.0,
    "amount_financed": 8730.0, "total_of_payments": 9768.11,
}
_CANONICAL = tuple(_COMPLETE)


def _offer(**overrides):
    values = dict(_COMPLETE, **overrides)
    return models.Offer(
        id=1, app_id=10, decision_id=10, fee_pct_used=0.03,
        apr=values["apr"], finance_charge=values["finance_charge"],
        monthly_payment=values["monthly_payment"],
        amount_financed=values["amount_financed"],
        total_of_payments=values["total_of_payments"],
    )


def _with_offer(offer):
    class _Session:
        def scalar(self, _stmt):
            return offer

    app.dependency_overrides[get_session] = lambda: _Session()
    return offer


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    app.dependency_overrides.pop(get_session, None)


def test_complete_offer_is_still_returned_normally():
    """Regression guard: the integrity check must not break the happy path."""
    _with_offer(_offer())

    resp = client.get("/applications/10/offer")

    assert resp.status_code == 200
    body = resp.json()
    assert body["apr"] == 5.946
    assert body["disclosure"]["finance_charge"] == 768.11
    assert body["schedule"], "a complete offer still renders its schedule"


@pytest.mark.parametrize("missing_field", _CANONICAL)
def test_any_missing_canonical_term_is_an_explicit_integrity_error(missing_field):
    """One case per canonical amount: apr, finance_charge, monthly_payment,
    amount_financed, total_of_payments."""
    _with_offer(_offer(**{missing_field: None}))

    resp = client.get("/applications/10/offer")

    assert resp.status_code == 409, f"a NULL {missing_field} must not render a disclosure"
    detail = resp.json()["detail"]
    assert missing_field in detail
    assert "incomplete" in detail.lower()


def test_a_missing_apr_never_renders_the_old_hardcoded_fallback_rate():
    """The specific regression: `offer.apr or 7.99` turned a NULL APR into a
    real-looking 7.99% loan. That number must not appear anywhere now."""
    _with_offer(_offer(apr=None))

    resp = client.get("/applications/10/offer")

    assert resp.status_code == 409
    assert "7.99" not in resp.text


def test_a_missing_amount_never_renders_as_zero():
    """`or 0` was just as dangerous as `or 7.99` -- a $0.00 finance charge is a
    disclosure claim, not an absence of one."""
    _with_offer(_offer(finance_charge=None))

    resp = client.get("/applications/10/offer")

    assert resp.status_code == 409
    body = resp.json()
    assert "disclosure" not in body, "no disclosure object may be returned at all"


def test_several_missing_terms_are_all_named():
    _with_offer(_offer(apr=None, monthly_payment=None))

    resp = client.get("/applications/10/offer")

    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert "apr" in detail and "monthly_payment" in detail
