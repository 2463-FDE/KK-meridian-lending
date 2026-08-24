"""Week 7's planning surface must describe the control that exists, and the
fixture it actually runs against.

Two failures this guards, both of which had already happened here in some form:

1. **Overstating the sample.** The Week 7 brief says the client handed over "a
   month of payments". What is committed is `db/settlement.csv`: seven days.
   The roadmap called the run "a sampled month", which is the client's framing
   borrowed as if it were the repository's evidence. The fix is not to
   manufacture rows until the fixture looks monthly -- it is to say what the file
   contains. So the numbers in the document are checked against the file.

2. **Leaving an item bare open.** Three Week 7 items are not built, and each is
   not-built for a different reason: a client decision (the fuzzy double-fund
   window), an operations decision (where a page goes), and one the brief itself
   made optional (an error-rate SLO, offered as an alternative to the break
   alert). "Open" for all three would read as three pieces of missing code. Each
   has to carry its classification.

Everything asserted here is an artefact, a symbol, a cited alert name or a number
recomputed from the fixture -- never a sentence. Prose gets rewritten; those do
not, and a check that fails on ordinary rewording teaches people to delete it.
"""
import csv
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
ROADMAP = REPO / "docs" / "ROADMAP.md"
SETTLEMENT = REPO / "db" / "settlement.csv"
ALERTS = REPO / "monitoring" / "alerts.yml"
RECONCILIATION = (REPO / "services" / "servicing-service" / "app"
                  / "reconciliation.py")


def _read(path: pathlib.Path) -> str:
    assert path.is_file(), f"missing: {path}"
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def week7() -> str:
    text = _read(ROADMAP)
    return text[text.index("## Week 7 —"):text.index("## Week 8 —")]


@pytest.fixture(scope="module")
def settlement():
    """(row count, first date, last date) as the committed fixture has them."""
    with SETTLEMENT.open(newline="", encoding="utf-8") as handle:
        rows = [row for row in csv.DictReader(handle) if row.get("settlement_date")]

    dates = sorted(row["settlement_date"].strip() for row in rows)
    return len(rows), dates[0], dates[-1]


# --------------------------------------------------------------------------
# The sample is described as it is, not as the brief described it.
# --------------------------------------------------------------------------

def test_the_week7_section_states_the_fixture_window_the_file_has(week7, settlement):
    count, first, last = settlement

    assert first in week7 and last in week7, (
        f"the Week 7 section does not state the window the fixture covers "
        f"({first} to {last}); a reader cannot tell what the control was run "
        f"against")
    assert str(count) in week7, (
        f"the Week 7 section does not state how many settlement rows the "
        f"fixture has ({count})")


def test_the_week7_section_does_not_call_a_seven_day_fixture_a_month(week7, settlement):
    """The client's own words may be quoted; the repository's evidence may not
    borrow them. A fixture spanning under 28 days is not a month."""
    _, first, last = settlement

    for claim in (r"sampled month", r"a month of (?:committed|fixture) data",
                  r"month-long (?:fixture|sample)"):
        for match in re.finditer(claim, week7, re.I):
            scope = week7[max(0, week7.rfind("\n\n", 0, match.start())):
                          week7.find("\n\n", match.end())]
            assert re.search(r"client|brief|handed over|attached", scope, re.I), (
                f"the Week 7 section calls the run {match.group(0)!r} outside any "
                f"quotation of the client's framing, while the fixture spans "
                f"{first} to {last}")


# --------------------------------------------------------------------------
# The week's two deliverables, and the artefacts they are claimed against.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("artefact", [
    "db/migrations/0043_correlation_id.sql",
    "db/migrations/0041_payments_processor_ref.sql",
    "db/migrations/0034_reconciliation_runs.sql",
    "services/servicing-service/app/reconcile_job.py",
    "services/servicing-service/app/reconcile_scheduler.py",
    "services/servicing-service/tests/test_double_capture_is_not_detected_yet.py",
    "monitoring/alerts.yml",
])
def test_the_artefacts_week7_claims_exist(artefact):
    assert (REPO / artefact).exists(), f"Week 7 claims {artefact}, which is gone"


def test_the_reconciliation_metrics_the_section_cites_are_emitted(week7):
    body = _read(RECONCILIATION)

    for metric in ("servicing_reconciliation_last_run_ok",
                   "servicing_reconciliation_last_success_timestamp"):
        assert metric in week7, f"the Week 7 section does not name {metric}"
        assert metric in body, (
            f"the Week 7 section cites {metric}, which nothing emits any more -- "
            f"an alert on an absent metric is silence dressed as coverage")


def test_the_alerts_the_section_cites_are_defined(week7):
    defined = set(re.findall(r"- alert:\s*(\w+)", _read(ALERTS)))
    assert defined, "no alert rules found at all"

    cited = {name for name in defined if name in week7}
    assert cited, (
        f"the Week 7 section cites none of the reconciliation alerts that exist "
        f"({sorted(defined)}), so its alert claim rests on nothing")

    for name in re.findall(r"\bReconciliation[A-Z]\w+", week7):
        assert name in defined, (
            f"the Week 7 section cites alert {name}, which is not in "
            f"monitoring/alerts.yml")


def test_the_week7_section_states_a_dated_closed_status(week7):
    """A dated status alone is not enough: this section already carries a
    "Status (2026-08-05): Partial" block, kept deliberately as the record of
    what was true then. Removing the current one left that older block
    satisfying a bare "is there a dated status" check -- mutation testing found
    it. So the date and the verdict have to travel together."""
    dated = list(re.finditer(r"Status \((\d{4}-\d{2}-\d{2})\)", week7))
    assert dated, "the Week 7 section carries no dated current-status statement"

    closed = [m for m in dated
              if re.search(r"is closed|✅ Closed",
                           week7[m.start():m.start() + 1500], re.I)]
    assert closed, (
        "no dated status in the Week 7 section says the required delivery is "
        f"closed; dated statuses found: {[m.group(1) for m in dated]}")


# --------------------------------------------------------------------------
# What is not built is classified, and by whom.
# --------------------------------------------------------------------------

def test_the_double_fund_gap_is_client_deferred_and_points_at_its_decision(week7):
    assert "CLIENT-DEFERRED" in week7, (
        "the fuzzy double-fund gap is not classified; 'open' would read as "
        "missing code rather than a missing client decision")
    assert "D22" in week7, (
        "the section does not cite the register entry holding the deferral and "
        "the three questions it needs answered")

    debt = _read(REPO / "docs" / "DEBT.md")
    d22 = debt[debt.index("| **D22**"):]
    d22 = d22[:d22.index("\n|")] if "\n|" in d22 else d22
    assert re.search(r"deferred", d22, re.I), (
        "DEBT.md D22 no longer records the deferral the roadmap points at")


def test_alert_delivery_is_ops_blocked_rather_than_claimed(week7):
    assert "OPS-BLOCKED" in week7, (
        "alert delivery to a human is not classified")
    assert "Alertmanager" in week7, (
        "the section does not name what is missing, so a reader cannot tell "
        "whether a firing alert reaches anyone")

    # And the claim has to match the compose file: if an Alertmanager is ever
    # wired up, this row is the one that goes stale first.
    compose = _read(REPO / "docker-compose.yml")
    assert "alertmanager" not in compose.lower(), (
        "docker-compose.yml now runs an Alertmanager, so 'OPS-BLOCKED' in the "
        "roadmap is out of date")


def test_the_error_rate_slo_is_marked_optional_not_missing(week7):
    """The brief says "one alert on a reconciliation break OR an error-rate
    SLO". The break alert exists, so the SLO is an alternative that was not
    taken -- not an unmet requirement."""
    assert re.search(r"error-rate", week7, re.I), (
        "the section does not mention the error-rate SLO the brief offered as "
        "an alternative")
    assert re.search(r"OPTIONAL", week7), (
        "the error-rate SLO is not marked optional, so it reads as an unmet "
        "acceptance item")


def test_the_superseded_status_paragraph_keeps_its_fence(week7):
    """Week 7 keeps its 2026-08-05 status paragraph verbatim -- "🟡 Partial",
    "both deliverables are still ⬜ Open" -- because rewriting it would erase
    the gap rather than close it. The whole thing rests on the blockquote above
    it saying it is superseded. Delete that blockquote and the section reads as
    a live claim that neither deliverable landed, which is the failure mode this
    week's own history is about.
    """
    stale = re.search(r"deliverables are still \*\*⬜ Open\*\*", week7)
    if stale is None:
        pytest.skip("the 2026-08-05 status paragraph is no longer quoted here")

    before = week7[:stale.start()]
    fence = [line for line in before.splitlines() if line.startswith(">")]
    assert fence, (
        "the superseded 2026-08-05 status paragraph has no blockquote fence "
        "above it, so its 'still Open' reads as current")
    assert any(re.search(r"superseded", line, re.I) for line in fence[-8:]), (
        "the blockquote above the 2026-08-05 status paragraph no longer says it "
        "is superseded")


def test_no_week7_item_is_left_bare_open(week7):
    """Every not-built row carries a classification. Bare "Open" is what this
    week's status looked like for two weeks after both deliverables landed."""
    classified = re.compile(
        r"(CLIENT-DEFERRED|OPS-BLOCKED|OPTIONAL|VENDOR-BLOCKED|CLIENT-BLOCKED|"
        r"DEFERRED)")
    historical = re.compile(
        r"(?i)(superseded|dated (?:discovery )?evidence|as of \d{4}-\d{2}-\d{2}|"
        r"no longer live status|previously|\bbefore\s+(?:PR\s+)?#\d+|"
        r"what client handed over)")

    for row in week7.splitlines():
        if not row.lstrip().startswith("|"):
            continue
        for cell in row.split("|"):
            if re.search(r"⬜|still (?:fully )?open|\bTODO\b", cell, re.I):
                assert classified.search(cell) or historical.search(cell), (
                    f"a Week 7 row is left open with no classification and no "
                    f"historical marker in its own cell:\n{cell.strip()[:300]}")
