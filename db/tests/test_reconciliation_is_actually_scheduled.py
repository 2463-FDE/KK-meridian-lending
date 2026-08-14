"""The reconciliation control must be scheduled and watched by this repository.

D7's failure mode is a control that silently is not running. Before this, the
repository shipped `python -m app.reconcile_job` plus a runbook paragraph telling
operators to wire cron themselves -- so a normal `docker compose up` kept
answering /health while reconciliation never ran once. The command existed, the
documentation existed, and the control did not.

These tests read the deployment files, because that is where the difference
between "documented" and "running" lives. They are deliberately about wiring, not
behaviour: the comparison logic is tested in servicing-service, and a scheduler
that is perfectly correct but absent from compose protects nobody.
"""
import pathlib

import pytest

yaml = pytest.importorskip("yaml")

REPO = pathlib.Path(__file__).resolve().parents[2]
COMPOSE = REPO / "docker-compose.yml"
PROM = REPO / "monitoring" / "prometheus.yml"
ALERTS = REPO / "monitoring" / "alerts.yml"
RUNBOOK = REPO / "docs" / "runbook.md"

SUCCESS_METRIC = "servicing_reconciliation_last_success_timestamp"


def _compose():
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))


def test_a_scheduler_service_exists():
    services = _compose()["services"]
    assert "reconciliation" in services, (
        "docker compose defines no reconciliation service, so a default "
        "deployment never runs the control"
    )


def test_the_scheduler_is_not_hidden_behind_a_profile():
    """A control an operator must remember to enable is the same defect.

    `docker compose up` with no arguments starts services that declare no
    `profiles` key. One with a profile is opt-in, which puts the control back
    where it started.
    """
    svc = _compose()["services"]["reconciliation"]
    assert not svc.get("profiles"), (
        f"the reconciliation service is behind profiles={svc.get('profiles')!r}, "
        "so `docker compose up` does not start it"
    )


def test_the_scheduler_restarts_rather_than_dying_on_a_finding():
    """The job exits 1 on a breach and 2 on a control failure.

    Without a restart policy, the first breach stops the container and the
    control is dead from then on -- the loudest possible finding silencing the
    thing that found it.
    """
    svc = _compose()["services"]["reconciliation"]
    assert svc.get("restart") in ("unless-stopped", "always"), (
        f"restart={svc.get('restart')!r}: a non-zero exit is a finding to read, "
        "not a reason to stop reconciling"
    )


def test_the_scheduler_entry_point_exists():
    svc = _compose()["services"]["reconciliation"]
    command = " ".join(svc["command"]) if isinstance(svc["command"], list) else svc["command"]
    assert "reconcile_scheduler" in command, f"unexpected command: {command!r}"

    module = REPO / "services" / "servicing-service" / "app" / "reconcile_scheduler.py"
    assert module.is_file(), "the scheduler service runs a module that does not exist"


def test_the_scheduler_reads_the_same_settlement_file_as_the_service():
    """A scheduler pointed at a different file would reconcile the wrong thing
    while looking healthy."""
    services = _compose()["services"]
    def mounts(name):
        return {v.split(":")[1] for v in services[name].get("volumes", []) if ":" in v}
    assert "/app/data/settlement.csv" in mounts("reconciliation"), (
        "the scheduler does not mount the settlement file"
    )
    assert mounts("reconciliation") >= {"/app/data/settlement.csv"}


def test_prometheus_loads_the_alert_rules():
    prom = yaml.safe_load(PROM.read_text(encoding="utf-8"))
    assert prom.get("rule_files"), "prometheus.yml declares no rule_files"

    mounted = {
        v.split(":")[1]
        for v in _compose()["services"]["prometheus"].get("volumes", [])
        if ":" in v
    }
    for rule_file in prom["rule_files"]:
        assert rule_file in mounted, (
            f"prometheus.yml loads {rule_file}, which is not mounted into the "
            "container -- the rules would silently not exist"
        )


def test_the_staleness_rule_watches_the_success_metric():
    """The specific rule D7 needs.

    A run that stops happening produces no failures at all, so staleness is the
    only signal. A rules file that watched only breaches would leave the
    original failure mode wide open.
    """
    rules = yaml.safe_load(ALERTS.read_text(encoding="utf-8"))
    exprs = [
        r["expr"]
        for g in rules["groups"] for r in g["rules"]
    ]
    assert any(SUCCESS_METRIC in e and "time()" in e for e in exprs), (
        f"no rule compares {SUCCESS_METRIC} against time(), so a control that "
        "stopped running would never be noticed"
    )
    assert any(f"absent({SUCCESS_METRIC})" in e.replace(" ", "") for e in exprs), (
        "no rule fires when the metric is missing entirely -- a control that "
        "has never succeeded emits no series, so the staleness rule cannot fire"
    )


def test_every_alert_names_a_runbook():
    rules = yaml.safe_load(ALERTS.read_text(encoding="utf-8"))
    for g in rules["groups"]:
        for r in g["rules"]:
            ann = r.get("annotations", {})
            assert ann.get("runbook"), f"{r['alert']} has no runbook annotation"
            assert (REPO / ann["runbook"]).exists(), (
                f"{r['alert']} cites {ann['runbook']}, which does not exist"
            )


def test_the_docs_do_not_claim_notifications_that_do_not_exist():
    """The rules genuinely fire. Nothing routes them.

    There is no Alertmanager in this compose file, so a firing alert has to be
    looked at. Claiming otherwise is the class of defect this control exists to
    remove, and the runbook is where someone would read the claim.
    """
    compose_services = set(_compose()["services"])
    assert "alertmanager" not in compose_services, (
        "an alertmanager now exists -- update the runbook, which currently says "
        "nothing is routed"
    )
    text = RUNBOOK.read_text(encoding="utf-8").lower()
    assert "no alertmanager" in text, (
        "the runbook does not disclose that no Alertmanager exists, so a reader "
        "would assume firing alerts reach somebody"
    )
