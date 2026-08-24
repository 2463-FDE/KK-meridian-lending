"""The contractual note rate is the server's to set, not the caller's.

Both frontends held `const OFFER_RATE_PCT = 7.99` and posted it into offer
creation, so the note rate written onto a real loan was whatever the client sent.
The same figure also defaulted in this service's `OfferIn` and in
disclosure-service's, which made five copies of a contractual term -- and the copy
that reached the borrower's loan was the one furthest from any authority.

There is no authority here for risk-based pricing of any kind: not by score, not
by income, not by DTI, not by employment, and not by anything a model produces.
There is one configured training rate, and these tests are about *who decides
it*, not about what it is -- the value is read from configuration rather than
asserted, so a demo run at a different rate does not fail its own test suite.
"""
import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app

client = TestClient(app)


def test_the_pricing_endpoint_reports_the_configured_rate():
    body = client.get("/applications/pricing").json() if False else \
        client.get("/pricing").json()

    assert body["note_rate_pct"] == pytest.approx(config.DEMO_NOTE_RATE_PCT)


def test_the_pricing_endpoint_says_it_is_not_a_pricing_policy():
    """A number alone invites being read as an underwritten rate. The response
    carries what it is, so a caller rendering it as "your rate" is contradicted
    by the payload it rendered."""
    body = client.get("/pricing").json()

    assert body["is_production_pricing_policy"] is False
    assert body["source"] == config.NOTE_RATE_SOURCE == "training_default"
    assert "APR" in body["note"], (
        "the response does not distinguish the note rate from the disclosed APR")


def test_the_pricing_endpoint_needs_no_session():
    """It serves the apply flow, which an applicant reaches before there is
    anything to authenticate, and it says nothing about a person."""
    assert client.get("/pricing").status_code == 200


def test_a_caller_may_omit_the_rate_entirely():
    """The normal path: the browser asks for an offer, the server prices it.

    Asserted on the request model rather than through the route, because the
    route then reads the application from PostgreSQL -- and a test that needed a
    database to prove a field is optional would skip in the unit job, which is
    the "skip reads like a pass" failure this repository keeps finding.
    """
    from app.routers.offers import OfferIn

    parsed = OfferIn(app_id=999_999, principal=5000, term_months=48)

    assert parsed.annual_rate_pct is None, (
        "the request still carries a rate of its own when the caller sent none")


def test_a_caller_sending_the_servers_own_rate_is_accepted():
    """Backward compatibility: a client that still sends the current figure keeps
    working rather than breaking on an upgrade."""
    from app.routers.offers import OfferIn

    parsed = OfferIn(app_id=999_999, principal=5000, term_months=48,
                     annual_rate_pct=config.DEMO_NOTE_RATE_PCT)

    assert parsed.annual_rate_pct == pytest.approx(config.DEMO_NOTE_RATE_PCT)


def test_a_rate_that_differs_only_in_float_noise_is_accepted():
    """7.99 is not exactly representable, and a caller that round-trips it
    through JSON must not be told it is trying to reprice the loan. The
    comparison is in basis points for exactly this reason."""
    from app.routers.offers import OfferIn

    noisy = float(f"{config.DEMO_NOTE_RATE_PCT:.10f}")

    assert OfferIn(app_id=1, principal=5000, term_months=48,
                   annual_rate_pct=noisy).annual_rate_pct == pytest.approx(noisy)


def test_no_risk_based_pricing_input_is_accepted():
    """The shape of the refusal matters as much as the refusal.

    Nothing authorises pricing by score, income, DTI, employment or model
    output, so the offer request has no field for any of them -- and `OfferIn`
    would silently drop one that arrived. Asserted on the schema rather than on a
    request, because the absence is the point.
    """
    from app.routers.offers import OfferIn

    assert set(OfferIn.model_fields) == {
        "app_id", "principal", "annual_rate_pct", "term_months"}, (
        "the offer request grew a field -- if it prices the loan by anything "
        "about the applicant, no client decision authorises it")
