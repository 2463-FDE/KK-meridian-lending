"""Every metric an alert rule names must be one the service actually emits.

`monitoring/alerts.yml` is the only thing standing between a reconciliation
control that stopped working and nobody noticing. A rule that references a
metric name the code does not publish never fires, never errors, and never
appears anywhere as a problem -- Prometheus evaluates it against an empty series
and moves on. The rules page stays green because there is nothing to be red
about.

That is the same failure D7 is about, one layer up: a control whose silence is
indistinguishable from health.

The existing guards in `test_reconciliation_is_actually_scheduled.py` cover the
rules FILE -- that Prometheus loads it, that it is mounted, that the staleness
rule exists, that every alert names a runbook. None of them checks the other
direction: that the names inside the expressions correspond to anything real.
They match today. Nothing enforces that they keep matching, and a rename in
`reconciliation.py` would take the alerting with it silently.

Deliberately narrow. This does not assert WHICH metrics should be alerted on --
that is a monitoring judgement, and `test_the_staleness_rule_watches_the_success
_metric` already pins the one rule D7 requires. It asserts only that every name
used resolves to a published one.
"""
import pathlib
import re

import pytest
import yaml

REPO = pathlib.Path(__file__).resolve().parents[2]
ALERTS = REPO / "monitoring" / "alerts.yml"
RECONCILIATION = REPO / "services" / "servicing-service" / "app" / "reconciliation.py"

#: PromQL functions and keywords that appear where a metric name would.
_NOT_METRICS = frozenset({
    "absent", "time", "rate", "increase", "sum", "avg", "min", "max", "count",
    "by", "without", "on", "ignoring", "group_left", "group_right", "offset",
    "and", "or", "unless", "bool", "for", "if",
})

#: A bare identifier in an expression. Metric names in this repository are
#: snake_case with a service prefix; the filter below drops PromQL's own words.
_IDENT = re.compile(r"\b([a-z_][a-z0-9_]{3,})\b")


def _rules():
    return yaml.safe_load(ALERTS.read_text(encoding="utf-8"))


def _alert_exprs():
    return [(r["alert"], r["expr"])
            for g in _rules()["groups"] for r in g["rules"]]


def _referenced_metrics():
    """Every identifier in every alert expression that looks like a metric."""
    found = set()
    for _name, expr in _alert_exprs():
        for ident in _IDENT.findall(expr):
            if ident not in _NOT_METRICS:
                found.add(ident)
    return found


def _published_metrics():
    """Metric names `reconciliation.py` registers with the Prometheus client.

    Read from the source rather than by importing and scraping a registry: the
    collector needs a database to produce a sample, and a test that silently
    skipped without one would be the exact failure this file is about.
    """
    return set(re.findall(r'"(servicing_[a-z_]+)"',
                          RECONCILIATION.read_text(encoding="utf-8")))


def test_there_are_rules_and_metrics_to_compare():
    """Guard the guard. Two empty sets satisfy any subset assertion."""
    assert _alert_exprs(), "alerts.yml declares no rules"
    assert _referenced_metrics(), "no rule expression names anything metric-shaped"
    assert _published_metrics(), (
        "no metric names were found in reconciliation.py -- the extraction "
        "broke, and every assertion below would pass vacuously"
    )


@pytest.mark.parametrize("alert,expr", _alert_exprs(), ids=lambda v: str(v)[:40])
def test_every_metric_an_alert_names_is_published(alert, expr):
    """A rule watching a metric nobody emits is silence dressed as coverage."""
    published = _published_metrics()
    used = {i for i in _IDENT.findall(expr) if i not in _NOT_METRICS}

    unknown = {u for u in used if u.startswith("servicing_")} - published
    assert not unknown, (
        f"alert {alert} watches {sorted(unknown)}, which reconciliation.py does "
        f"not publish. Prometheus evaluates that against an empty series and "
        f"never fires, so the control could stop working and the rules page "
        f"would stay green. Published: {sorted(published)}"
    )


def test_every_alert_watches_at_least_one_service_metric():
    """A rule built entirely from PromQL functions and constants is not
    watching this system at all."""
    for alert, expr in _alert_exprs():
        used = {i for i in _IDENT.findall(expr) if i.startswith("servicing_")}
        assert used, f"alert {alert} names no servicing metric: {expr!r}"


def test_the_breach_rule_watches_the_run_outcome():
    """The other rule D7 needs, pinned the way staleness already is.

    Staleness catches a control that stopped running. This catches one that ran
    and found money missing. Losing either leaves half the failure mode
    uncovered, and losing this one is the half that looks healthiest.
    """
    exprs = [e for _a, e in _alert_exprs()]

    assert any("servicing_reconciliation_last_run_ok" in e for e in exprs), (
        "no rule watches the run outcome, so a reconciliation that ran and "
        "breached its threshold would page nobody"
    )
    assert "servicing_reconciliation_last_run_ok" in _published_metrics()
