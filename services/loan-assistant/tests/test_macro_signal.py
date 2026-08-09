"""The one grounded external signal on the officer summary (app/macro.py).

Week 1-4 client review: the summary "collects almost everything from inside the
application". These tests cover the four properties that make the fix safe to
run rather than just present -- it fails open, it does not leak the applicant,
it does not author its own citation, and it does not spend the API budget.

Nothing here touches the network. `BlsMacroProvider` is exercised against a
faked httpx, so a CI run neither depends on BLS being up nor consumes any of the
25 requests/day the unauthenticated v1 API allows.
"""
import time

import httpx
import pytest

from app import llm_client, macro
from app.macro import BlsMacroProvider, MacroSignal, StubMacroProvider

_APP = {
    "id": 1, "amount": 18000, "term_months": 48, "purpose": "debt consolidation",
    "income": 82000, "employer": "Fictional Testing Co", "job_title": "QA Analyst",
    "employment_years": 3,
}

_LIVE_SHAPE = {
    "status": "REQUEST_SUCCEEDED",
    "Results": {"series": [{
        "seriesID": "LNS14000000",
        "data": [
            {"year": "2026", "period": "M06", "periodName": "June", "latest": "true", "value": "4.2"},
            {"year": "2026", "period": "M05", "periodName": "May", "value": "4.3"},
        ],
    }]},
}


class _FakeResponse:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)

    def json(self):
        return self._payload


@pytest.fixture(autouse=True)
def _isolate_provider(monkeypatch):
    """Every test gets its own provider and the feature switched on, so no test
    inherits another's cache or the module-level default."""
    monkeypatch.setattr(macro, "MACRO_ENABLED", True, raising=False)
    monkeypatch.setattr(macro, "provider", StubMacroProvider(), raising=False)
    yield


# --- parsing the real response shape -----------------------------------------

def test_parses_the_documented_bls_response(monkeypatch):
    """Shape captured from a real BLS v1 response, not invented."""
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(_LIVE_SHAPE))
    signal = BlsMacroProvider().fetch()

    assert signal is not None
    assert signal.value == 4.2
    assert signal.period == "June 2026"          # the LATEST month, not May
    assert signal.series_id == "LNS14000000"
    assert signal.source == "U.S. Bureau of Labor Statistics"


def test_the_citation_names_the_value_period_and_series():
    """A figure an officer cannot trace back is not a citation."""
    cite = MacroSignal(
        "U.S. Bureau of Labor Statistics", "LNS14000000",
        "US unemployment rate (seasonally adjusted)", 4.2, "percent",
        "June 2026", "https://example.test",
    ).cite()
    assert "4.2%" in cite
    assert "June 2026" in cite
    assert "LNS14000000" in cite
    assert "Bureau of Labor Statistics" in cite


# --- fails open ---------------------------------------------------------------

@pytest.mark.parametrize("failure", [
    pytest.param(lambda *a, **k: (_ for _ in ()).throw(httpx.ConnectError("down")), id="unreachable"),
    pytest.param(lambda *a, **k: _FakeResponse({}, status=500), id="http-500"),
    pytest.param(lambda *a, **k: _FakeResponse({"status": "REQUEST_NOT_PROCESSED"}), id="api-error"),
    pytest.param(lambda *a, **k: _FakeResponse({"status": "REQUEST_SUCCEEDED", "Results": {}}), id="shape-changed"),
])
def test_every_provider_failure_returns_none_rather_than_raising(monkeypatch, failure):
    """Opposite of the bureau and scorer calls, on purpose: this is context next
    to a summary, not a decision input. A BLS outage must never stop an officer
    seeing their applicant's file."""
    monkeypatch.setattr(httpx, "get", failure)
    assert BlsMacroProvider().fetch() is None


def test_a_summary_is_still_produced_when_the_signal_is_unavailable(monkeypatch):
    monkeypatch.setattr(macro, "current_signal", lambda: None)
    prompt = llm_client._build_prompt(_APP, None)
    assert "External context" not in prompt
    assert "debt consolidation" in prompt      # the summary itself is unaffected


def test_a_past_ttl_value_is_still_served_after_a_failure(monkeypatch):
    """DELIBERATE REVERSAL of an earlier rule, with the reason.

    This test previously asserted the opposite -- that a past-TTL figure is
    never served -- on the grounds that "a figure captioned 'June 2026' that is
    actually months old is worse than no figure, the officer cannot tell it is
    stale."

    That premise is false. The caption IS the staleness disclosure: MacroSignal
    carries `period`, and `cite()` prints it, so a figure fetched an hour ago
    and one fetched yesterday both read "June 2026" and neither claims anything
    about retrieval time. The series itself only updates monthly. Withholding a
    true, correctly-labelled figure during a BLS outage removed real context and
    bought nothing.

    Changed because the module's behaviour changed, not to make a failing test
    pass: serving the last known value is what "fail open" means, and the bound
    below is what keeps it honest.
    """
    provider = BlsMacroProvider()
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(_LIVE_SHAPE))
    assert provider.fetch().value == 4.2

    monkeypatch.setattr(macro, "MACRO_CACHE_TTL_SECONDS", 0, raising=False)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse({}, status=500))

    served = provider.fetch()
    assert served is not None, "fail-open must serve the last known figure"
    assert served.value == 4.2
    # The caption still names the period the figure describes, which is the
    # whole basis for serving it.
    assert "June 2026" in served.cite()
    assert provider.stale_served_count == 1


def test_a_value_older_than_the_stale_window_is_dropped(monkeypatch):
    """The bound. Fail-open is not "show the last number forever" -- past
    MACRO_STALE_SERVE_SECONDS the citation disappears rather than ageing
    silently on the screen."""
    provider = BlsMacroProvider()
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(_LIVE_SHAPE))
    assert provider.fetch().value == 4.2

    monkeypatch.setattr(macro, "MACRO_CACHE_TTL_SECONDS", 0, raising=False)
    monkeypatch.setattr(macro, "MACRO_STALE_SERVE_SECONDS", 0, raising=False)
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse({}, status=500))

    assert provider.fetch() is None


# --- budget -------------------------------------------------------------------

def test_repeat_calls_hit_the_cache_not_the_api(monkeypatch):
    """The unauthenticated v1 API allows 25 requests/day per IP. One call per
    summary would exhaust that in a morning, so caching is correctness here,
    not an optimisation."""
    calls = {"n": 0}

    def counting_get(*a, **k):
        calls["n"] += 1
        return _FakeResponse(_LIVE_SHAPE)

    monkeypatch.setattr(httpx, "get", counting_get)
    provider = BlsMacroProvider()
    for _ in range(25):
        assert provider.fetch().value == 4.2
    assert calls["n"] == 1, f"{calls['n']} API calls for 25 summaries -- cache is not working"


# --- privacy -------------------------------------------------------------------

def test_no_applicant_data_is_sent_to_the_third_party(monkeypatch):
    """The request must carry a fixed series id and nothing else. A future
    switch to a state-level series would leak coarse location on every summary
    view; this pins the current property so that change has to be deliberate."""
    seen = {}

    def capture(url, *a, **k):
        seen["url"] = url
        seen["kwargs"] = k
        return _FakeResponse(_LIVE_SHAPE)

    monkeypatch.setattr(httpx, "get", capture)
    BlsMacroProvider().fetch()

    assert seen["url"] == "https://api.bls.gov/publicAPI/v1/timeseries/data/LNS14000000"
    assert "params" not in seen["kwargs"] and "json" not in seen["kwargs"]
    # Nothing identifying anywhere in the outbound call.
    blob = seen["url"] + repr(seen["kwargs"])
    for leak in ("18000", "82000", "Fictional", "QA Analyst", "debt consolidation"):
        assert leak not in blob


# --- the model does not author the citation -------------------------------------

def test_the_prompt_labels_the_signal_as_not_from_the_applicant():
    signal = StubMacroProvider().fetch()
    prompt = llm_client._build_prompt(_APP, signal)
    assert "NOT supplied by the applicant" in prompt
    assert "do not" in prompt.lower()          # instructed against inventing others
    assert signal.cite() in prompt


def test_the_cited_figure_comes_from_the_provider_not_the_model(monkeypatch):
    """If the model restates the rate differently in its prose, the officer must
    still see the published number. Same rule as applicant_name."""
    signal = MacroSignal(
        "U.S. Bureau of Labor Statistics", "LNS14000000",
        "US unemployment rate (seasonally adjusted)", 4.2, "percent",
        "June 2026", "https://example.test",
    )
    monkeypatch.setattr(macro, "current_signal", lambda: signal)
    monkeypatch.setattr(llm_client, "make_client", lambda: object())
    # The model "reports" a different, wrong rate in its own text.
    monkeypatch.setattr(llm_client, "call_api", lambda c, p: (
        '{"loan_amount": 18000, "term_months": 48, "purpose": "debt consolidation",'
        ' "risk_tier": "medium", "summary": "Unemployment is 11.9% which is alarming.",'
        ' "flags": []}'
    ))

    result = llm_client.summarize_application(dict(_APP, applicant={"name": "Robin Fictional"}))

    assert len(result.external_signals) == 1
    cited = result.external_signals[0]
    assert cited.value == 4.2, "the citation followed the model instead of the provider"
    assert "11.9" not in cited.citation
    assert cited.series_id == "LNS14000000"


def test_no_signal_means_no_citation_rather_than_a_placeholder(monkeypatch):
    monkeypatch.setattr(macro, "current_signal", lambda: None)
    monkeypatch.setattr(llm_client, "make_client", lambda: object())
    monkeypatch.setattr(llm_client, "call_api", lambda c, p: (
        '{"loan_amount": 18000, "term_months": 48, "purpose": "debt consolidation",'
        ' "risk_tier": "medium", "summary": "ok", "flags": []}'
    ))

    result = llm_client.summarize_application(dict(_APP, applicant={"name": "Robin Fictional"}))
    assert result.external_signals == []


# --- cost ------------------------------------------------------------------------

def test_the_signal_is_inside_the_cost_guard_not_outside_it():
    """The signal adds tokens, so the guard has to weigh the prompt that is
    actually sent. Measured: +92 tokens (+38.8%) on a representative
    application, against a 2,000-token guard."""
    signal = MacroSignal(
        "U.S. Bureau of Labor Statistics", "LNS14000000",
        "US unemployment rate (seasonally adjusted)", 4.2, "percent",
        "June 2026", "https://api.bls.gov/publicAPI/v1/timeseries/data/LNS14000000",
    )
    without = llm_client._estimate_tokens(llm_client._SYSTEM + llm_client._build_prompt(_APP, None))
    with_signal = llm_client._estimate_tokens(llm_client._SYSTEM + llm_client._build_prompt(_APP, signal))

    delta = with_signal - without
    assert delta > 0, "the signal reached the prompt but not the token estimate"
    assert delta < 200, f"signal cost {delta} tokens -- larger than the measured 92, re-measure"
    assert with_signal < llm_client.MAX_INPUT_TOKENS


# --- the stale-serve window is measured PAST the TTL -------------------------

def _cached_provider(monkeypatch, age_seconds: float, ttl: float, stale: float):
    """A provider holding one figure fetched `age_seconds` ago."""
    monkeypatch.setattr(macro, "MACRO_CACHE_TTL_SECONDS", ttl, raising=False)
    monkeypatch.setattr(macro, "MACRO_STALE_SERVE_SECONDS", stale, raising=False)
    provider = BlsMacroProvider()
    provider._cached = MacroSignal(
        source="U.S. Bureau of Labor Statistics", series_id="LNS14000000",
        label="unemployment rate", value=4.2, unit="%", period="June 2026",
        url="https://data.bls.gov/timeseries/LNS14000000",
    )
    provider._cached_at = time.monotonic() - age_seconds
    return provider


def test_the_stale_window_starts_where_the_ttl_ends(monkeypatch):
    """config.py documents it as time PAST MACRO_CACHE_TTL_SECONDS.

    The comparison used to be against the total age, which silently turned the
    documented 24 hours past a 6-hour TTL into 18 hours of stale serving. Here:
    TTL 10s, stale window 100s, figure 105s old -- 95s past the TTL, so inside
    the window and servable. The old comparison (105 < 100) dropped it.
    """
    provider = _cached_provider(monkeypatch, age_seconds=105, ttl=10, stale=100)
    assert provider._servable_stale() is not None


def test_a_stale_window_shorter_than_the_ttl_still_serves(monkeypatch):
    """The configuration that used to disable the feature outright.

    With a 100s TTL and a 10s stale window, ANY figure old enough to be stale
    was already older than the stale window under the old comparison, so nothing
    was ever served past its TTL -- the fail-open path silently absent in
    exactly the outage it exists for.
    """
    provider = _cached_provider(monkeypatch, age_seconds=105, ttl=100, stale=10)
    assert provider._servable_stale() is not None


def test_past_the_combined_window_the_citation_is_dropped(monkeypatch):
    """Still bounded. Stale-serving is a grace period, not an indefinite cache."""
    provider = _cached_provider(monkeypatch, age_seconds=200, ttl=10, stale=100)
    assert provider._servable_stale() is None


# --- a series is only cited as what it actually is ---------------------------

def test_an_unconfigured_series_yields_no_signal_rather_than_a_borrowed_label(monkeypatch):
    """Overriding MACRO_SERIES_ID must not relabel another series' number.

    The label and unit were hardcoded, so pointing the provider at, say, a CPI
    series returned that value captioned "US unemployment rate (seasonally
    adjusted)" in percent -- a wrong figure shown to an officer, and given to
    the model, as GROUNDED context. Since grounding is the whole point, a
    caption we cannot justify is worse than no signal. Reviewed on PR #13.
    """
    monkeypatch.setattr(macro, "MACRO_SERIES_ID", "CUUR0000SA0", raising=False)
    called = {"n": 0}

    def _transport(*a, **k):
        called["n"] += 1
        return _FakeResponse(_LIVE_SHAPE)

    monkeypatch.setattr(httpx, "get", _transport)

    assert BlsMacroProvider().fetch() is None
    # And it refuses BEFORE spending one of the 25 daily requests to learn
    # something the configuration already determined.
    assert called["n"] == 0


def test_a_response_for_an_unexpected_series_is_not_captioned(monkeypatch):
    """Metadata is keyed on what BLS returned, not on what we asked for.

    A redirect, a proxy, or an upstream change that answers with a different
    series must not inherit the configured series' caption.
    """
    other = {
        "status": "REQUEST_SUCCEEDED",
        "Results": {"series": [{
            "seriesID": "CUUR0000SA0",
            "data": [{"year": "2026", "period": "M06", "periodName": "June",
                      "latest": "true", "value": "317.6"}],
        }]},
    }
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(other))
    assert BlsMacroProvider().fetch() is None


def test_the_supported_series_is_still_captioned_from_its_metadata(monkeypatch):
    """The other half: the configured default keeps working, from the table."""
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(_LIVE_SHAPE))
    signal = BlsMacroProvider().fetch()

    assert signal is not None
    label, unit = macro._SERIES_METADATA["LNS14000000"]
    assert signal.label == label
    assert signal.unit == unit
