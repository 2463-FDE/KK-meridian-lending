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

def _d22() -> str:
    """The D22 row of the register, on its own.

    One row per line in this table, so the entry is the line that starts
    with its id. Split on a newline escape here once, and the escaping cost
    more than reading the line did.
    """
    for line in _read(REPO / "docs" / "DEBT.md").splitlines():
        if line.startswith("| **D22**"):
            return line
    raise AssertionError("docs/DEBT.md has no D22 row at all")


def test_the_double_fund_gap_is_no_longer_recorded_as_deferred(week7):
    """This test used to assert the opposite, and the inversion is the point.

    It required `CLIENT-DEFERRED` in the Week 7 section and the word "deferred"
    in D22, which was true and worth pinning while the decision was outstanding.
    The client answered on 2026-08-24. A guard that still demanded the deferral
    would be holding the documents at a status that stopped being true -- the
    exact defect this file exists to catch, pointed the wrong way.
    """
    assert "CLIENT-DEFERRED" not in week7, (
        "the Week 7 section still classifies something as CLIENT-DEFERRED. The "
        "double-fund decision arrived on 2026-08-24; if a DIFFERENT item is now "
        "deferred, this guard needs to name it rather than leaving the row to "
        "read as the old one")

    d22 = _d22()
    assert not re.search(r"Deferred pending", d22, re.I), (
        "DEBT.md D22 still opens as deferred pending a client decision. The "
        "decision was received on 2026-08-24")
    assert "2026-08-24" in d22, (
        "D22 does not date the decision it now records, so a reader cannot tell "
        "which instruction it is describing")


def test_d22_says_the_answer_was_review_and_not_a_break(week7):
    """The part most likely to be summarised into something false.

    The client did not unblock the fifth break kind D22 proposed -- they replaced
    it with a review signal put to a human. A document recording "detection
    built" without that distinction leaves a reader expecting reconciliation to
    raise a break, and finding a passing test that says it does not.
    """
    d22 = _d22()

    assert re.search(r"review", d22, re.I), (
        "D22 does not say the decision was to flag for human review")
    assert re.search(r"(no break|raises no break|not.{0,40}break)", d22, re.I), (
        "D22 does not say reconciliation still raises no break on this shape, "
        "so the entry and `test_double_capture_is_not_detected_yet.py` read as "
        "contradicting each other")

    for disposition in ("confirmed_duplicate", "legitimate_distinct_payment",
                        "requires_further_review"):
        assert disposition in d22, (
            "D22 does not name the authorised disposition %r; the three are the "
            "whole of what a reviewer may record" % disposition)


def test_the_pin_test_and_the_register_agree_that_no_break_is_intended(week7):
    """The passing-tripwire problem.

    `test_double_capture_is_not_detected_yet.py` still passes, and its assertions
    are unchanged, because the decided behaviour IS no break. That is correct and
    it is also a trap: a reader who finds a test called "not detected yet" that
    passes concludes the gap is open. So the file has to say the decision was
    made, in its own words, rather than relying on a register entry elsewhere.
    """
    pin = _read(REPO / "services" / "servicing-service" / "tests"
                / "test_double_capture_is_not_detected_yet.py")

    assert "2026-08-24" in pin, (
        "the pin test does not mention the decision that settled what it pins")
    assert not re.search(r"D22 records the question, the owner and", pin), (
        "the pin test still describes D22 as holding an unanswered question")
    assert re.search(r"(decided behaviour|by decision, not by omission)", pin, re.I), (
        "the pin test does not say that raising no break is the decided "
        "behaviour, so a passing tripwire still reads as an open gap")


def test_the_review_queue_destination_is_recorded_as_delivered(week7):
    """Alert delivery is two questions now, and only one of them is blocked.

    The client authorised the in-app queue as the destination for a payment
    review candidate and prohibited every external channel. So "nothing reaches
    a person" is no longer true of review items, and a section that said it would
    understate what exists while overstating what is blocked.
    """
    assert re.search(r"in-app|/reconciliation", week7, re.I), (
        "the Week 7 section does not name the in-app queue as the authorised "
        "destination, so review candidates read as having nowhere to go")
    # The TOKEN, on the alert-delivery row itself. An earlier version of this
    # assertion accepted the word "prohibit" anywhere in the Week 7 section, and
    # a mutation that downgraded the row to plain OPS-BLOCKED and softened
    # "prohibits" to "discourages" still passed -- the word survived in a summary
    # paragraph two lines below. A classification that is only implied by nearby
    # prose is the thing this file exists to refuse.
    delivery = [line for line in week7.splitlines()
                if "Alert delivery to a human" in line and "breaks" in line]
    assert delivery, "the Week 7 section has no alert-delivery row for breaks"
    assert "CLIENT-PROHIBITED" in delivery[0], (
        "the reconciliation-break alert row does not carry CLIENT-PROHIBITED. "
        "Without it the block reads as purely an operations decision a developer "
        "could resolve, when the client has also forbidden every external "
        "channel before the freeze")

    # And the token has to be defined where the others are, or it is a label a
    # reader cannot resolve.
    debt = _read(REPO / "docs" / "DEBT.md")
    header = debt[:debt.index("| ID |")]
    assert "CLIENT-PROHIBITED" in header, (
        "CLIENT-PROHIBITED is used but not defined in DEBT.md's classification "
        "header, so it reads as a synonym for CLIENT-BLOCKED -- which is the one "
        "thing it is not")



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


def test_the_debt_register_and_the_roadmap_agree_about_d7(week7):
    """The register and the roadmap describe the same control, so they must not
    disagree about whether it exists.

    D7 read "Partly fixed" while the Week 7 status block called the control
    closed. Both were describing the same code: the control is implemented and
    tested, and the missing piece is a destination for a firing alert, which no
    amount of work in this repository produces. "Partly fixed" reads as unbuilt
    code, and a reader comparing the two documents cannot tell which is right.
    """
    debt = _read(REPO / "docs" / "DEBT.md")
    row = debt[debt.index("| **D7**"):]
    row = row[:row.index("\n|")] if "\n|" in row else row

    cells = row.split("|")
    status = cells[3] if len(cells) > 3 else row

    # The label a reader sees first, not merely a label somewhere in a very long
    # cell. Mutation testing: restoring "**Partly fixed.**" as the opener passed
    # while "OPS-BLOCKED" survived further down the same cell.
    opener = status.strip()[:220]
    assert not re.match(r"\*\*Partly", opener), (
        "D7's status opens with 'Partly ...', which reads as unfinished code "
        "when the control is implemented and the gap is a deployment decision")
    assert re.search(r"IMPLEMENTED", opener), (
        "D7's status does not open by saying the control is implemented")
    assert re.search(r"(CLIENT|VENDOR|OPS)-BLOCKED|CLIENT-DEFERRED|"
                     r"CLIENT-PROHIBITED", opener), (
        "D7's status does not open with a classification for what is missing")

    # And the clause about alert delivery specifically has to carry the label --
    # a header label alone let "(1) Alert delivery to a human -- open" pass.
    delivery = re.search(r"Alert delivery[^.]{0,120}", status, re.I)
    assert delivery, "D7 no longer names alert delivery as the missing piece"
    assert "OPS-BLOCKED" in delivery.group(0), (
        f"D7 names alert delivery without classifying it: "
        f"{delivery.group(0).strip()[:160]}")
    assert "OPS-BLOCKED" in week7, (
        "the roadmap no longer classifies alert delivery, so D7 now points at "
        "a label that is not there")

    # Neither document may claim the thing that does not exist.
    for label, text in (("D7", row), ("the Week 7 section", week7)):
        for match in re.finditer(r"Alertmanager", text):
            clause = text[max(0, text.rfind(".", 0, match.start())):
                          (text.find(".", match.end()) + 1) or len(text)]
            assert re.search(r"\b(no|not|without|missing|deployed nowhere|"
                             r"would be|needs)\b", clause, re.I), (
                f"{label} mentions an Alertmanager without saying there is "
                f"none: {clause.strip()[:200]}")


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
    # Matched on the durable part of the sentence, not on its marker.
    #
    # This searched for the literal `deliverables are still **⬜ Open**`, and
    # when that row's glyph changed to the historical marker the search missed
    # and `pytest.skip` made this guard go QUIET -- a fence check that stopped
    # checking, discoverable only because the suite's skip count went from 0 to
    # 1. That is the same defect this file exists to catch, in the file itself.
    #
    # An absent paragraph is now a FAILURE rather than a skip. Preserving it is
    # the convention; if it has gone, that is the finding, not a reason to stop
    # looking.
    stale = re.search(r"deliverables (?:are|were) still \*\*[^*]*[Oo]pen", week7)
    assert stale is not None, (
        "Week 7 no longer quotes its 2026-08-05 status paragraph. It is kept "
        "verbatim on purpose -- rewriting it erases the gap rather than closing "
        "it -- so its absence is a finding, not a reason to skip this check")

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
    # `CLIENT-PROHIBITED` is in this list, and review of PR #82 is why. The
    # token was defined in DEBT.md's header and used on the alert-delivery row,
    # but not added here -- so it passed only because that row is ALSO
    # OPS-BLOCKED. A row carrying the prohibition alone would have read as
    # unclassified, which is the failure this test exists to catch, defeated by
    # the addition of a new label rather than by anything going stale.
    classified = re.compile(
        r"(CLIENT-DEFERRED|OPS-BLOCKED|OPTIONAL|VENDOR-BLOCKED|CLIENT-BLOCKED|"
        r"CLIENT-PROHIBITED|DEFERRED)")
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
