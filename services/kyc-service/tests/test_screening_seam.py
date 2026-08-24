"""The screening seam: what it refuses, and what it never leaks.

Spec 0004 §4/§5 and ADR 0012. No provider exists, so every case here runs
against the stub or against a fake transport -- which is the point: the
idempotency, list-version and fail-closed contracts are what this repository
would REQUIRE of a vendor, and they are testable without one.

Nothing here asserts a screening verdict about a person. The stub carries no
names, and the only "match" in this file is triggered by an obviously synthetic
marker.
"""
import httpx
import pytest

from app import screening
from app.screening import (
    CLEAR,
    ERROR,
    POTENTIAL_MATCH,
    STUB_MATCH_MARKER,
    HttpScreeningProvider,
    ScreeningResult,
    ScreeningUnavailable,
    StubScreeningProvider,
)


@pytest.fixture
def stub():
    provider = StubScreeningProvider()
    yield provider
    provider.reset()


# --------------------------------------------------------------------------
# The result type refuses to exist without its evidence.
# --------------------------------------------------------------------------

def test_a_result_needs_a_list_version():
    """"Clear against the SDN List" without saying which day's list is a claim
    that cannot be reproduced, so it is not evidence and cannot be built."""
    with pytest.raises(ScreeningUnavailable):
        ScreeningResult(outcome=CLEAR, list_version="", reference_id="r-1",
                        match_count=0)


def test_a_result_needs_a_provider_reference():
    with pytest.raises(ScreeningUnavailable):
        ScreeningResult(outcome=CLEAR, list_version="v1", reference_id="",
                        match_count=0)


def test_an_unknown_outcome_is_refused():
    """Three outcomes exist. A fourth would be a verdict this repository
    invented on the provider's behalf."""
    with pytest.raises(ScreeningUnavailable):
        ScreeningResult(outcome="probably_fine", list_version="v1",
                        reference_id="r-1", match_count=0)


def test_a_negative_match_count_is_refused():
    with pytest.raises(ScreeningUnavailable):
        ScreeningResult(outcome=CLEAR, list_version="v1", reference_id="r-1",
                        match_count=-1)


@pytest.mark.parametrize("outcome", [POTENTIAL_MATCH, ERROR])
def test_only_clear_is_clear(outcome):
    """A potential match MUST NOT auto-resolve to clear (spec 0004 §3.2), and
    neither may an error. `is_clear` exists so no caller writes that comparison
    itself and gets it wrong somewhere."""
    result = ScreeningResult(outcome=outcome, list_version="v1",
                             reference_id="r-1", match_count=1)

    assert not result.is_clear


def test_a_clear_result_is_clear():
    assert ScreeningResult(outcome=CLEAR, list_version="v1", reference_id="r-1",
                           match_count=0).is_clear


# --------------------------------------------------------------------------
# The stub: idempotency, and no list data.
# --------------------------------------------------------------------------

def test_an_ordinary_subject_screens_clear(stub):
    result = stub.screen(name="Test Subject", dob="1980-01-01",
                         address="1 Test Street", request_key="req-1")

    assert result.is_clear
    assert result.match_count == 0
    assert stub.screen_count == 1


def test_the_synthetic_marker_produces_a_potential_match(stub):
    """The match path has to be testable, and it is reached by a marker rather
    than by a name -- committing anything that resembles a real SDN entry would
    create a file people mistake for the list (spec 0004 §4)."""
    result = stub.screen(name=f"Subject {STUB_MATCH_MARKER}", dob=None,
                         address=None, request_key="req-2")

    assert result.outcome == POTENTIAL_MATCH
    assert not result.is_clear
    assert result.match_count == 1


def test_a_replayed_request_key_returns_the_original_screen(stub):
    """The bureau boundary shipped without this and an ambiguous timeout meant a
    second billed pull. Here a duplicate would also write a second piece of
    evidence about one subject, and two evidence rows that disagree are worse
    than one."""
    first = stub.screen(name="Test Subject", dob=None, address=None,
                        request_key="req-3")
    second = stub.screen(name="Test Subject", dob=None, address=None,
                         request_key="req-3")

    assert second is first or second == first
    assert stub.screen_count == 1, "the replay performed a second real screen"


def test_a_different_request_key_screens_again(stub):
    """A genuinely new onboarding step is a new screen, against whatever the
    list says now."""
    stub.screen(name="Test Subject", dob=None, address=None, request_key="req-4")
    stub.screen(name="Test Subject", dob=None, address=None, request_key="req-5")

    assert stub.screen_count == 2


def test_a_screen_without_a_request_key_is_refused(stub):
    with pytest.raises(ScreeningUnavailable):
        stub.screen(name="Test Subject", dob=None, address=None, request_key="")


def test_the_stub_says_it_is_a_stub(stub):
    """A stub screen that reported a plausible list date would be
    indistinguishable from a real one in the audit record -- the mistake the
    `-stub` model-version suffix already exists to prevent."""
    result = stub.screen(name="Test Subject", dob=None, address=None,
                         request_key="req-6")

    assert "stub" in result.list_version
    assert result.reference_id.startswith("stub-")


def test_the_module_carries_no_list_like_data():
    """The marker is deliberately not a name. If it ever becomes one, this
    fails before the file starts being mistaken for the list."""
    assert STUB_MATCH_MARKER.isupper()
    assert " " not in STUB_MATCH_MARKER
    assert "STUB" in STUB_MATCH_MARKER


def test_no_threshold_constant_exists():
    """A number here would look like a control and be a guess: the cutoff is
    COMPLIANCE-BLOCKED on the false-negative appetite and VENDOR-BLOCKED on the
    provider's scoring semantics (spec 0004 §3.3)."""
    suspicious = [name for name in dir(screening)
                  if any(word in name.upper()
                         for word in ("THRESHOLD", "CUTOFF", "MIN_SCORE",
                                      "CONFIDENCE"))]

    assert not suspicious, f"screening.py defines {suspicious}"


def test_the_subjects_identity_never_reaches_a_log_line(stub, caplog):
    """Executed, not read: the CIP route already follows this rule and this
    boundary handles the same fields."""
    with caplog.at_level("DEBUG"):
        stub.screen(name="Gloria Testperson", dob="1979-04-02",
                    address="88 Example Road", request_key="req-7")

    logged = "\n".join(record.getMessage() for record in caplog.records)
    for leak in ("Gloria", "Testperson", "1979-04-02", "88 Example Road"):
        assert leak not in logged, f"the screening log line carries {leak!r}"
    assert "req-7" in logged, "the log line cannot be correlated at all"


# --------------------------------------------------------------------------
# The HTTP shape: the two defects the bureau boundary shipped with.
# --------------------------------------------------------------------------

def _transport(handler):
    return httpx.MockTransport(handler)


def _provider_with(handler, monkeypatch):
    """An HttpScreeningProvider whose client uses a fake transport."""
    real_client = httpx.Client

    def _client(*args, **kwargs):
        kwargs["transport"] = _transport(handler)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(screening.httpx, "Client", _client)
    return HttpScreeningProvider(base_url="https://screening.invalid",
                                 api_key="test-key")


def test_identity_data_goes_in_the_body_not_the_query_string(monkeypatch):
    """This is the defect `bureau.py` was written to fix: an SSN in a query
    string lands in the provider's access logs and every proxy in between."""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["query"] = request.url.query.decode()
        seen["body"] = request.read().decode()
        seen["headers"] = dict(request.headers)
        return httpx.Response(200, json={"outcome": CLEAR,
                                         "list_version": "2026-06-01",
                                         "reference_id": "prov-1",
                                         "match_count": 0})

    provider = _provider_with(handler, monkeypatch)
    provider.screen(name="Test Subject", dob="1980-01-01",
                    address="1 Test Street", request_key="req-8")

    assert seen["query"] == "", f"identity data in the query string: {seen['url']}"
    assert "Test Subject" in seen["body"]
    assert seen["headers"]["idempotency-key"] == "req-8", (
        "the caller's key is not forwarded, so the provider cannot deduplicate "
        "a retried request server-side")


def test_a_provider_error_fails_closed(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"detail": "unavailable"})

    provider = _provider_with(handler, monkeypatch)

    with pytest.raises(ScreeningUnavailable):
        provider.screen(name="Test Subject", dob=None, address=None,
                        request_key="req-9")


def test_a_transport_failure_fails_closed(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    provider = _provider_with(handler, monkeypatch)

    with pytest.raises(ScreeningUnavailable):
        provider.screen(name="Test Subject", dob=None, address=None,
                        request_key="req-10")


def test_a_response_with_no_list_version_fails_closed(monkeypatch):
    """A provider that answers "clear" without saying which list it used has
    not produced evidence, and the absence must not be filled in locally."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"outcome": CLEAR,
                                         "reference_id": "prov-2",
                                         "match_count": 0})

    provider = _provider_with(handler, monkeypatch)

    with pytest.raises(ScreeningUnavailable):
        provider.screen(name="Test Subject", dob=None, address=None,
                        request_key="req-11")


def test_an_unparseable_outcome_fails_closed(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"outcome": "maybe",
                                         "list_version": "2026-06-01",
                                         "reference_id": "prov-3",
                                         "match_count": 0})

    provider = _provider_with(handler, monkeypatch)

    with pytest.raises(ScreeningUnavailable):
        provider.screen(name="Test Subject", dob=None, address=None,
                        request_key="req-12")


def test_a_provider_failure_does_not_leak_the_response_body(monkeypatch, caplog):
    """A hit's payload is third-party list data about named individuals. The
    exception type is enough for an operator; the body is not theirs to keep."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="matched: SOME PERSON, dob 1962-01-01")

    provider = _provider_with(handler, monkeypatch)

    with caplog.at_level("DEBUG"):
        with pytest.raises(ScreeningUnavailable) as exc:
            provider.screen(name="Test Subject", dob=None, address=None,
                            request_key="req-13")

    logged = "\n".join(record.getMessage() for record in caplog.records)
    for leak in ("SOME PERSON", "1962-01-01"):
        assert leak not in logged
        assert leak not in str(exc.value)


def test_an_unusable_payload_is_not_quoted_back(monkeypatch, caplog):
    """The other half of the leak rule, and the one that was missing.

    A failed HTTP response never reaches the parser, so the earlier test covers
    only that path. A 200 whose body is the wrong shape DOES reach it -- and
    interpolating that body into the error puts candidate names into logs, which
    is the whole reason the raw response is not stored. Mutation testing found
    this gap: adding `+ str(payload)` to the parse failure passed everything
    else.
    """
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"candidates": ["SOME PERSON"],
                                         "dob": "1962-01-01"})

    provider = _provider_with(handler, monkeypatch)

    with caplog.at_level("DEBUG"):
        with pytest.raises(ScreeningUnavailable) as exc:
            provider.screen(name="Test Subject", dob=None, address=None,
                            request_key="req-14")

    logged = "\n".join(record.getMessage() for record in caplog.records)
    for leak in ("SOME PERSON", "1962-01-01", "candidates"):
        assert leak not in str(exc.value), f"the error quotes {leak!r}"
        assert leak not in logged, f"the log line quotes {leak!r}"


def test_an_http_screen_without_a_request_key_is_refused(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("the provider must not be called at all")

    provider = _provider_with(handler, monkeypatch)

    with pytest.raises(ScreeningUnavailable):
        provider.screen(name="Test Subject", dob=None, address=None,
                        request_key="")


# --------------------------------------------------------------------------
# Selection: a stub outside development is a configuration error.
# --------------------------------------------------------------------------

def test_the_stub_is_selected_only_in_a_development_environment(monkeypatch):
    monkeypatch.setattr(screening, "ALLOW_SCREENING_STUB", True)
    assert isinstance(screening.provider(), StubScreeningProvider)

    monkeypatch.setattr(screening, "ALLOW_SCREENING_STUB", False)
    provider = screening.provider()
    assert isinstance(provider, HttpScreeningProvider), (
        "an unset ENVIRONMENT is a reachable production state, and it must not "
        "get the stub")
