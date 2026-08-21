"""The Week 8 brief's first question, answered by measurement.

*"Across many decisions, how many distinct adverse-action reasons does the
model actually emit?"* Until now that was answerable only by opening
`decision.py` and counting constants — which tells you what the code CAN emit,
not what it DID. The difference is the whole point: a vendor whose behaviour
drifts, or a version that collapses onto one reason, shows up in the second and
never in the first.

Spec 0003 §1.3. Fixtures throughout; `decision_events` is stubbed, so no
database and no model call.
"""
import pytest

from app import db, reason_distribution

LOW_BUREAU = "Low credit bureau score relative to lending criteria"
LOW_INCOME = "Insufficient income relative to lending criteria"


def _stub(monkeypatch, rows):
    captured = {}

    def _query(sql, params=None):
        captured["sql"] = sql
        captured["params"] = params
        return rows

    monkeypatch.setattr(db, "query", _query)
    return captured


def _row(version, codes, decision="deny"):
    return {"model_version": version, "reason_codes": codes, "decision": decision}


# --------------------------------------------------------------------------
# The measurement itself.
# --------------------------------------------------------------------------

def test_it_counts_distinct_reasons_and_their_frequency(monkeypatch):
    _stub(monkeypatch, [
        _row("v1", [LOW_BUREAU]), _row("v1", [LOW_BUREAU]),
        _row("v1", [LOW_INCOME]),
    ])

    report = reason_distribution.adverse_reason_distribution()

    assert len(report["versions"]) == 1
    v1 = report["versions"][0]
    assert v1["distinct_reasons"] == 2
    assert v1["reason_frequency"] == {LOW_BUREAU: 2, LOW_INCOME: 1}
    assert v1["decisions"] == 3


def test_versions_are_never_merged(monkeypatch):
    """A distribution mixing two model versions describes neither, which is why
    spec 0003 makes the grouping mandatory rather than optional."""
    _stub(monkeypatch, [
        _row("v1", [LOW_BUREAU]),
        _row("v2-stub", [LOW_INCOME]), _row("v2-stub", [LOW_INCOME]),
    ])

    report = reason_distribution.adverse_reason_distribution()

    versions = {v["model_version"]: v for v in report["versions"]}
    assert set(versions) == {"v1", "v2-stub"}
    assert versions["v1"]["distinct_reasons"] == 1
    assert versions["v2-stub"]["reason_frequency"] == {LOW_INCOME: 2}


def test_a_denial_with_no_reason_is_counted_not_skipped(monkeypatch):
    """Should be zero. It is the Reg B defect itself, so it has to be visible
    rather than quietly filtered out of the denominator."""
    _stub(monkeypatch, [
        _row("v1", [LOW_BUREAU]), _row("v1", []), _row("v1", None),
        _row("v1", ["   "]),
    ])

    v1 = reason_distribution.adverse_reason_distribution()["versions"][0]

    assert v1["missing_reason"] == 3
    assert v1["decisions"] == 4
    assert v1["distinct_reasons"] == 1


def test_only_the_principal_reason_is_counted(monkeypatch):
    """A notice states the principal reason. Counting every code would describe
    the model's internal signalling rather than what applicants were told."""
    _stub(monkeypatch, [_row("v1", [LOW_BUREAU, LOW_INCOME])])

    v1 = reason_distribution.adverse_reason_distribution()["versions"][0]

    assert v1["reason_frequency"] == {LOW_BUREAU: 1}
    assert v1["distinct_reasons"] == 1


def test_a_missing_model_version_is_labelled_not_dropped(monkeypatch):
    _stub(monkeypatch, [_row(None, [LOW_BUREAU])])

    assert reason_distribution.adverse_reason_distribution()[
        "versions"][0]["model_version"] == "unknown"


def test_frequencies_are_ordered_most_common_first(monkeypatch):
    _stub(monkeypatch, [_row("v1", [LOW_INCOME])] + [_row("v1", [LOW_BUREAU])] * 3)

    order = list(reason_distribution.adverse_reason_distribution()
                 ["versions"][0]["reason_frequency"])

    assert order == [LOW_BUREAU, LOW_INCOME]


def test_an_empty_corpus_is_an_empty_report_not_a_zero_verdict(monkeypatch):
    """"No decisions in this window" and "no distinct-reason problem" are
    different statements and must not render identically."""
    _stub(monkeypatch, [])

    report = reason_distribution.adverse_reason_distribution()

    assert report["versions"] == []
    assert "distinct_reasons" not in report


# --------------------------------------------------------------------------
# The window, which the report has to state about itself.
# --------------------------------------------------------------------------

def test_the_window_is_echoed_back_including_when_it_is_open(monkeypatch):
    _stub(monkeypatch, [])

    assert reason_distribution.adverse_reason_distribution()["window"] == {
        "since": None, "until": None}


def test_the_window_reaches_the_query(monkeypatch):
    captured = _stub(monkeypatch, [])

    report = reason_distribution.adverse_reason_distribution(
        since="2026-06-01", until="2026-06-30")

    assert "occurred_at >=" in captured["sql"]
    assert "2026-06-01" in captured["params"]
    assert "2026-06-30" in captured["params"]
    assert report["window"] == {"since": "2026-06-01", "until": "2026-06-30"}


def test_the_until_bound_includes_its_own_day(monkeypatch):
    """`until=2026-06-30` meaning "up to 2026-06-30 00:00" would silently drop a
    day's decisions -- the same off-by-one the reconciliation window had to fix."""
    captured = _stub(monkeypatch, [])

    reason_distribution.adverse_reason_distribution(until="2026-06-30")

    assert "INTERVAL '1 day'" in captured["sql"]


# --------------------------------------------------------------------------
# What it must not do.
# --------------------------------------------------------------------------

def test_only_denials_are_counted(monkeypatch):
    """An approval carries no adverse-action reason, so counting approvals as
    "missing reason" would bury the real signal under every approval."""
    captured = _stub(monkeypatch, [])

    reason_distribution.adverse_reason_distribution()

    assert "decision = ANY" in captured["sql"]
    assert captured["params"][0] == ["deny"]


def test_no_applicant_identifier_is_selected(monkeypatch):
    """Aggregate only. Nothing here should be tieable back to a person."""
    captured = _stub(monkeypatch, [])

    reason_distribution.adverse_reason_distribution()

    select = captured["sql"].split("FROM")[0].lower()
    for identifier in ("app_id", "applicant", "annual_income", "bureau_score",
                       "model_score", "requested_amount"):
        assert identifier not in select, (
            f"{identifier} is selected into an aggregate report")


def test_the_report_states_no_threshold_or_verdict(monkeypatch):
    """Spec 0003: what counts as too few distinct reasons is a compliance
    judgement this repository has no authority to make."""
    _stub(monkeypatch, [_row("v1", [LOW_BUREAU])])

    report = reason_distribution.adverse_reason_distribution()

    for verdict_key in ("passed", "failed", "flagged", "threshold", "compliant",
                        "fair", "verdict"):
        assert verdict_key not in report, f"the report renders a verdict: {verdict_key}"
        assert verdict_key not in report["versions"][0]
