"""The one grounded external signal on the officer summary (app/macro.py).

Week 1-4 client review: the summary "collects almost everything from inside the
application". These tests cover the four properties that make the fix safe to
run rather than just present -- it fails open, it does not leak the applicant,
it does not author its own citation, and it does not spend the API budget.

Nothing here touches the network. `BlsMacroProvider` is exercised against a
faked httpx, so a CI run neither depends on BLS being up nor consumes any of the
25 requests/day the unauthenticated v1 API allows.
"""
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


def test_a_stale_value_is_never_served_after_a_failure(monkeypatch):
    """A figure captioned 'June 2026' that is actually months old is worse than
    no figure -- the officer cannot tell it is stale. Omit instead."""
    provider = BlsMacroProvider()
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(_LIVE_SHAPE))
    assert provider.fetch().value == 4.2

    monkeypatch.setattr(macro, "MACRO_CACHE_TTL_SECONDS", 0, raising=False)
    monkeypatch.setattr(provider, "_fresh", lambda: False)
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
