"""What a caller of `POST /offers` may and may not send about the rate.

These need no database on purpose. `test_offer_repair_real_postgres.py` skips
entirely without `DATABASE_URL`, so when `annual_rate` briefly became required
its 27 requests would have 422'd before reaching the repair path and the suite
still reported green -- 31 skipped reads exactly like 31 passed in a tail-of-run
count. The rate contract is a property of the schema, so it is asserted against
the schema, in the job that always runs.

The contract itself: the note rate is the server's (`config.DEMO_NOTE_RATE_PCT`,
the same variable origination publishes at `GET /los/pricing`). A caller may
omit it, may echo it, and may not choose a different one.
"""
import pytest
from pydantic import ValidationError

from app import config
from app.schemas import OfferIn


def _shape(**overrides):
    body = {"application_id": 10, "principal": 9000.0, "term_months": 24}
    body.update(overrides)
    return body


def test_the_repair_caller_shape_is_accepted():
    """The exact body `test_offer_repair_real_postgres.py::_post` sends.

    This is the regression: that helper omits `annual_rate`, and a required
    field turned every repair test in the file into a validation error that no
    unit run could see.
    """
    parsed = OfferIn(**_shape())

    assert parsed.annual_rate is None, (
        "the request carries a rate of its own when the caller sent none -- a "
        "default here is a second copy of a contractual term")


def test_a_caller_may_echo_the_servers_own_rate():
    """Backward compatibility. Both real callers -- `disclosure_graph.py` and
    origination's offer route -- send `config.DEMO_NOTE_RATE_PCT`, so refusing
    it would break the only two clients this service has."""
    parsed = OfferIn(**_shape(annual_rate=config.DEMO_NOTE_RATE_PCT))

    assert parsed.annual_rate == pytest.approx(config.DEMO_NOTE_RATE_PCT)


def test_a_rate_that_differs_only_in_float_noise_is_accepted():
    """7.99 is not exactly representable, so a caller that round-trips the
    server's own figure through JSON must not be told it is repricing the loan.
    The comparison is in basis points for this reason."""
    noisy = float(f"{config.DEMO_NOTE_RATE_PCT:.10f}")

    assert OfferIn(**_shape(annual_rate=noisy)).annual_rate == pytest.approx(noisy)


def test_a_caller_choosing_a_different_rate_is_refused_not_ignored():
    """The refusal, and why it is a refusal.

    The handler prices from configuration, so a differing value could simply be
    dropped -- and that is worse: the caller would believe it had priced the
    loan at its number while the disclosure it got back said something else.
    """
    with pytest.raises(ValidationError) as err:
        OfferIn(**_shape(annual_rate=config.DEMO_NOTE_RATE_PCT + 4))

    assert "set by the server" in str(err.value)


def test_the_refusal_says_there_is_no_risk_based_pricing():
    """A caller sending a rate is usually trying to price by applicant. The
    message says no such pricing exists, rather than only that the field is
    rejected."""
    with pytest.raises(ValidationError) as err:
        OfferIn(**_shape(annual_rate=config.DEMO_NOTE_RATE_PCT + 4))

    assert "risk-based pricing" in str(err.value)


def test_the_schema_holds_no_default_rate():
    """The copy that was removed. A default is what a forgetful caller gets
    priced at, and it was `7.99` sitting in a request model -- one of the five
    copies of a contractual term this change collapsed."""
    default = OfferIn.model_fields["annual_rate"].default

    assert default is None, (
        f"OfferIn defaults annual_rate to {default!r}; the rate comes from "
        f"DEMO_NOTE_RATE_PCT, and a default beside it is a copy that can drift")
