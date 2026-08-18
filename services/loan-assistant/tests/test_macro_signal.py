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


def _signal():
    return MacroSignal(
        "U.S. Bureau of Labor Statistics", "LNS14000000",
        "US unemployment rate (seasonally adjusted)", 4.2, "percent",
        "June 2026", "https://example.test",
    )


def _summarize_with(monkeypatch, summary: str, flags="[]"):
    signal = _signal()
    monkeypatch.setattr(macro, "current_signal", lambda: signal)
    monkeypatch.setattr(llm_client, "make_client", lambda: object())
    monkeypatch.setattr(llm_client, "call_api", lambda c, p: (
        '{"loan_amount": 18000, "term_months": 48, "purpose": "debt consolidation",'
        f' "risk_tier": "medium", "summary": "{summary}",'
        f' "flags": {flags}}}'
    ))
    return llm_client.summarize_application(dict(_APP, applicant={"name": "Robin Fictional"}))


def test_the_cited_figure_comes_from_the_provider_not_the_model(monkeypatch):
    """If the model restates the rate differently in its prose, the officer must
    still see the published number. Same rule as applicant_name."""
    result = _summarize_with(
        monkeypatch,
        "Stable employment and adequate income. Unemployment is 11.9% which is alarming.",
    )

    assert len(result.external_signals) == 1
    cited = result.external_signals[0]
    assert cited.value == 4.2, "the citation followed the model instead of the provider"
    assert "11.9" not in cited.citation
    assert cited.series_id == "LNS14000000"


def test_a_contradicting_figure_never_reaches_the_officers_summary(monkeypatch):
    """The other half, and the reviewed defect.

    A correct citation is not enough while the prose beside it says something
    else: LoanSummaryCard renders both, so the officer was shown "unemployment
    is 11.9%" directly above the provider's cited 4.2%. A grounded figure that
    is contradicted on the same screen is worse than no figure at all, because
    the reader has no way to tell which one is the sourced one.
    """
    result = _summarize_with(
        monkeypatch,
        "Stable employment and adequate income. Unemployment is 11.9% which is alarming.",
    )

    assert "11.9" not in result.summary
    # The sentence that was actually about the application survives -- this is a
    # scalpel, not a mute button.
    assert "Stable employment" in result.summary


def test_an_accurate_restatement_is_left_alone(monkeypatch):
    """Only CONTRADICTIONS are removed.

    The prompt asks the model not to repeat the figure, but repeating it
    correctly misleads nobody, and stripping it would be the service editing
    prose for style rather than for truth.
    """
    result = _summarize_with(
        monkeypatch,
        "Unemployment at 4.2% is low. Income comfortably covers the payment.",
    )

    assert "4.2" in result.summary
    assert "Income comfortably covers" in result.summary


def test_a_qualitative_reference_is_left_alone(monkeypatch):
    """Words about the signal carry no figure to contradict."""
    result = _summarize_with(
        monkeypatch,
        "The labour market is soft, which raises repayment risk slightly.",
    )

    assert "labour market is soft" in result.summary


def test_a_contradicting_flag_is_removed_too(monkeypatch):
    """Flags are officer-facing prose as well, and were unguarded.

    The surviving flag used to be "Debt-to-income near the limit". That is now
    removed by `_strip_dti_claims` -- correctly, since this system holds no debt
    obligations and any such ratio is fabricated -- so it can no longer serve as
    the innocent bystander here. Swapped for a flag that is genuinely grounded
    in data the model is given, which is what this assertion always meant.
    """
    result = _summarize_with(
        monkeypatch,
        "Adequate income for the requested amount.",
        flags='["Unemployment at 11.9% is a concern", "Employment under one year"]',
    )

    assert all("11.9" not in f for f in result.flags)
    assert "Employment under one year" in result.flags


def test_a_summary_that_is_nothing_but_a_false_claim_fails_closed(monkeypatch):
    """Nothing honest is left to show, and inventing a replacement is authoring.

    Every other guardrail in this service fails closed; a summary whose entire
    content was a false statement about a published figure is not a summary.
    """
    with pytest.raises(llm_client.LLMResponseError):
        _summarize_with(monkeypatch, "Unemployment is 11.9% which is alarming.")


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


# --- claims the earlier guard let through ------------------------------------

def test_a_sentence_carrying_both_the_true_and_a_false_figure_is_removed(monkeypatch):
    """One matching number used to exempt the whole sentence.

    "Unemployment is 11.9%, while the cited series value is 4.2%" contradicts
    both the source and itself, and it survived intact next to the 4.2%
    citation because the check accepted a sentence as soon as ANY figure in it
    matched. Reviewed on PR #13.
    """
    result = _summarize_with(
        monkeypatch,
        "Income is adequate. Unemployment is 11.9%, while the cited series value is 4.2%.",
    )

    assert "11.9" not in result.summary
    assert "Income is adequate" in result.summary


def test_an_unrelated_number_in_a_signal_sentence_is_not_read_as_a_rate(monkeypatch):
    """The other direction: requiring every figure to match would be too blunt.

    An income or a loan amount beside an accurate rate is not a competing claim
    about the rate, so figures written as the signal's unit are what count.
    """
    result = _summarize_with(
        monkeypatch,
        "With unemployment at 4.2% and income of 82000, repayment looks comfortable.",
    )

    assert "82000" in result.summary
    assert "4.2%" in result.summary


def test_an_abbreviation_does_not_leave_an_orphan_fragment(monkeypatch):
    """`U.S.` is not the end of a sentence.

    The splitter cut "The U.S. unemployment rate is 11.9%." after the
    abbreviation, so removing the false claim left "The U.S." behind -- nonempty,
    so the fail-closed check passed, and malformed prose reached the officer.
    """
    result = _summarize_with(
        monkeypatch,
        "The U.S. unemployment rate is 11.9%. Income is adequate.",
    )

    assert "11.9" not in result.summary
    assert "The U.S." not in result.summary, "an orphaned abbreviation was left behind"
    assert result.summary.strip() == "Income is adequate."


def test_an_abbreviation_inside_a_kept_sentence_survives_intact(monkeypatch):
    """Masking the dot must not damage prose that is fine as it stands."""
    result = _summarize_with(
        monkeypatch,
        "The U.S. labour market is soft, which raises repayment risk slightly.",
    )

    assert "The U.S. labour market is soft" in result.summary


# --- the cost guard judges the application, not the optional citation --------

def test_the_signal_is_dropped_rather_than_failing_an_otherwise_valid_prompt(monkeypatch):
    """An optional extra must not be the reason a summary 400s.

    `purpose`, `employer` and `job_title` have no maximum length in
    origination's ApplicationIn, so a base prompt can sit just under the ceiling
    and the citation can push it over. The signal fails open everywhere else --
    disabled, unreachable, rate-limited -- and it fails open here too.
    Reviewed on PR #13.
    """
    signal = _signal()
    monkeypatch.setattr(macro, "current_signal", lambda: signal)
    monkeypatch.setattr(llm_client, "make_client", lambda: object())
    seen = {}

    def _capture(client, prompt):
        seen["prompt"] = prompt
        return (
            '{"loan_amount": 18000, "term_months": 48, "purpose": "debt consolidation",'
            ' "risk_tier": "medium", "summary": "Adequate income.", "flags": []}'
        )

    monkeypatch.setattr(llm_client, "call_api", _capture)

    app = dict(_APP, applicant={"name": "Robin Fictional"})
    base_tokens = llm_client._estimate_tokens(
        llm_client._SYSTEM + llm_client._build_prompt(app, None)
    )
    with_signal = llm_client._estimate_tokens(
        llm_client._SYSTEM + llm_client._build_prompt(app, signal)
    )
    # A ceiling the application clears on its own and the citation does not.
    monkeypatch.setattr(llm_client, "MAX_INPUT_TOKENS", (base_tokens + with_signal) // 2)

    result = llm_client.summarize_application(app)

    assert result.summary  # it answered rather than raising
    assert signal.cite() not in seen["prompt"], "the citation was sent anyway"
    assert result.external_signals == [], (
        "a citation was attached that the model was never shown"
    )


def test_an_application_too_large_on_its_own_still_fails_the_guard(monkeypatch):
    """Dropping the signal must not become a way to smuggle a huge payload past."""
    signal = _signal()
    monkeypatch.setattr(macro, "current_signal", lambda: signal)
    monkeypatch.setattr(llm_client, "make_client", lambda: object())
    monkeypatch.setattr(llm_client, "MAX_INPUT_TOKENS", 1)

    with pytest.raises(llm_client.LLMCostGuardError):
        llm_client.summarize_application(dict(_APP, applicant={"name": "Robin Fictional"}))


def test_the_unit_pattern_matches_a_spelled_out_percent(monkeypatch):
    """The unit pattern must recognise the words, not only the symbol.

    Its `\b` word boundaries were written as literal U+0008 backspace
    characters, so "4.2 percent" never matched and the check fell back to
    treating EVERY number in the sentence as a rate claim -- discarding an
    accurate sentence because an income sat next to the figure. Only the "%"
    alternative worked, which is why the earlier tests missed it. Reviewed on
    PR #13.

    Asserted on the pattern directly as well as through the service, so a
    regression names the cause rather than a symptom three layers away.
    """
    assert llm_client._UNIT_FIGURE_RE.findall("unemployment is 4.2 percent") == ["4.2"]
    assert llm_client._UNIT_FIGURE_RE.findall("unemployment is 11.9 pct") == ["11.9"]
    assert llm_client._UNIT_FIGURE_RE.findall("income of 82000") == []
    # A word that merely starts with "percent" is not the unit.
    assert llm_client._UNIT_FIGURE_RE.findall("rose 4.2 percentage points") == []


def test_a_spelled_out_rate_beside_an_income_survives(monkeypatch):
    """The user-visible half of the same defect."""
    result = _summarize_with(
        monkeypatch,
        "Unemployment is 4.2 percent while income is 82000, so repayment looks sound.",
    )

    assert "82000" in result.summary
    assert "4.2 percent" in result.summary


def test_a_spelled_out_contradiction_is_still_removed(monkeypatch):
    """...and the pattern working must not weaken the guard itself."""
    result = _summarize_with(
        monkeypatch,
        "Income is adequate. Unemployment is 11.9 percent, which is alarming.",
    )

    assert "11.9" not in result.summary
    assert "Income is adequate" in result.summary


# --- a topic word is not a licence to delete ---------------------------------

def test_a_sentence_about_the_applicant_is_not_treated_as_a_macro_claim(monkeypatch):
    """The reviewed sentence, verbatim.

    `labor` reached the topic words from the SOURCE name ("U.S. Bureau of Labor
    Statistics"), so this ordinary sentence about the applicant matched; the
    all-figures fallback then compared its `5` against the published 4.2 and
    deleted it. As the only sentence, that turned a valid summary into a 502.
    Reviewed on PR #13.
    """
    result = _summarize_with(
        monkeypatch,
        "The applicant has 5 years of labor experience and adequate income.",
    )

    assert result.summary == "The applicant has 5 years of labor experience and adequate income."


def test_the_publishers_name_alone_does_not_make_a_sentence_a_claim(monkeypatch):
    """Who published a figure is not what the figure is about."""
    result = _summarize_with(
        monkeypatch,
        "Bureau records show 12 months of continuous employment.",
    )

    assert "12 months" in result.summary


def test_a_unit_shaped_contradiction_is_still_removed(monkeypatch):
    """Narrowing must not disarm the guard."""
    result = _summarize_with(
        monkeypatch,
        "Income is adequate. Unemployment is 11.9% and rising.",
    )

    assert "11.9" not in result.summary
    assert "Income is adequate" in result.summary


def test_a_bare_number_beside_a_topic_word_is_a_stated_limitation(monkeypatch):
    """What dropping the fallback costs, asserted rather than left implicit.

    "unemployment is 11.9" with no unit is NOT treated as a claim, because a
    bare number beside a topic word is indistinguishable from a count of years
    or applications -- which is exactly how the deletion bug happened. The
    prompt asks the model not to restate the figure at all; this is the
    backstop for the form a reader would actually read as a rate. If this
    behaviour is ever tightened, this test should be the thing that changes.
    """
    result = _summarize_with(monkeypatch, "Unemployment is 11.9 and rising.")

    assert "11.9" in result.summary


# --- a percentage must belong to the signal before it is treated as a claim ---
#
# Reviewed on PR #13. Every unit-shaped figure in a topic-mentioning sentence was
# treated as a claim about the signal, so an unrelated percentage in the same
# breath -- a loan-to-income ratio -- was compared against the published rate and
# the sentence was deleted. As the only sentence, that answered 502 for prose
# that said nothing wrong.

def test_an_unrelated_percentage_beside_the_topic_word_survives(monkeypatch):
    """The reviewed sentence, verbatim."""
    result = _summarize_with(
        monkeypatch,
        "Unemployment remains relevant, while the requested loan is 22% of annual income.",
    )

    assert result.summary == (
        "Unemployment remains relevant, while the requested loan is 22% of annual income."
    ), "an unrelated loan-to-income percentage was read as an unemployment claim"


def test_a_correct_macro_figure_survives_an_unrelated_percentage(monkeypatch):
    """The worse half of the same defect.

    The sentence's macro figure was RIGHT and the sentence was still deleted,
    because the unrelated 22 was also compared against 4.2. A guard that removes
    accurate grounded prose is doing the opposite of its job.
    """
    result = _summarize_with(
        monkeypatch,
        "The loan is 22% of income and unemployment is 4.2%.",
    )

    assert "22%" in result.summary
    assert "4.2%" in result.summary


@pytest.mark.parametrize("sentence", [
    "Unemployment stands at 11.9% nationally.",
    "11.9% unemployment makes this risky.",
    "The U.S. unemployment rate is 11.9%.",
])
def test_a_contradiction_bound_to_the_topic_is_still_removed(monkeypatch, sentence):
    """Narrowing must not disarm the guard, in either word order.

    The figure is bound to the topic by proximity, so it has to work when the
    topic word precedes the number and when it follows it.
    """
    result = _summarize_with(monkeypatch, sentence + " Income is adequate.")

    assert "11.9" not in result.summary
    assert "Income is adequate" in result.summary


def test_a_ratio_sentence_with_no_topic_word_is_untouched(monkeypatch):
    """The control: nothing about this sentence concerns the signal."""
    result = _summarize_with(monkeypatch, "The payment is 31% of monthly income.")

    assert result.summary == "The payment is 31% of monthly income."


# --- the label phrasing the prompt itself supplies ----------------------------
#
# Reviewed on PR #13, after the topic-binding fix. A fixed four-token window
# looked back over "rate seasonally adjusted is" and never reached
# `unemployment`, so the model's own full-label phrasing -- the one most likely
# to appear, because that exact parenthetical label is in the prompt -- slipped
# a false 11.9% past the guard and onto the officer's screen beside the cited
# 4.2%. The binding now walks through label words, generic label words and
# connectors, and stops at the first word that is none of those.

@pytest.mark.parametrize("sentence", [
    "The US unemployment rate (seasonally adjusted) is 11.9%.",
    "The US unemployment rate (seasonally adjusted) is reported at 11.9%.",
    "The U.S. unemployment rate is currently about 11.9%.",
])
def test_a_contradiction_stated_with_the_full_label_is_removed(monkeypatch, sentence):
    """The reviewed phrasing, and two variants that put more words in the way."""
    result = _summarize_with(monkeypatch, sentence + " Income is adequate.")

    assert "11.9" not in result.summary, (
        "a false figure stated with the signal's own label survived"
    )
    assert "Income is adequate" in result.summary


def test_walking_through_label_words_still_stops_at_an_unrelated_subject(monkeypatch):
    """The other half: the walk must not reach across a different noun.

    `loan` is not a label word, a stopword or a connector, so the backward walk
    stops there and the 22% is left alone. Without this the widened binding would
    simply delete more prose than the fixed window did.
    """
    result = _summarize_with(
        monkeypatch,
        "Unemployment remains relevant, while the requested loan is 22% of annual income.",
    )

    assert "22%" in result.summary


# --- the citation has to be readable by the person it is for -------------------

def test_the_citation_url_is_a_human_page_not_the_api():
    """A "Verify at U.S. Bureau of Labor Statistics" link that returns raw JSON
    is not verification.

    The citation URL used to be the API endpoint -- literally the same address
    the fetch uses -- so following it produced a wall of JSON. The reader the
    citation exists for is a loan officer or a compliance reviewer, not a
    developer with a JSON viewer; for them, that link proved nothing.

    Found by clicking it.
    """
    from app import macro

    signal = macro.StubMacroProvider().fetch()
    assert signal.url.startswith("https://data.bls.gov/timeseries/"), (
        f"citation points at {signal.url!r}, which is not a page a person can read"
    )
    assert "api.bls.gov" not in signal.url, (
        "the citation points at the API endpoint, which returns raw JSON"
    )
    assert macro.MACRO_SERIES_ID in signal.url, (
        "the citation does not name the series it cites"
    )


def test_the_fetch_still_uses_the_api_endpoint():
    """Guard the guard. The two URLs are deliberately different, and changing
    the citation must not quietly change where the data comes from."""
    from app import macro

    assert macro._BLS_V1_URL.startswith("https://api.bls.gov/")
    assert macro._BLS_SERIES_PAGE.startswith("https://data.bls.gov/")
    assert macro._BLS_V1_URL != macro._BLS_SERIES_PAGE
