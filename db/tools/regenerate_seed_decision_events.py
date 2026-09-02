"""Regenerate the seeded `decision_events` rows from the seeded decision INPUTS.

    python db/tools/regenerate_seed_decision_events.py            # check only, exit 1 on drift
    python db/tools/regenerate_seed_decision_events.py --write     # rewrite the seed SQL

WHY THIS EXISTS
    A fresh `db/init` seeded 306 applications and 306 `decisions` rows and ZERO
    `decision_events`. Measured, not inferred:

        outcome   applications   with a decision_event
        approve            184                       0
        deny                62                       0
        refer               60                       0

    Every underwriting screen therefore reported, correctly, that it had no
    automated decision on record:

        Final application outcome       approve
        Automated model decision        not recorded
        Model version                   not recorded
        Underwriting model score        not recorded
        Credit bureau score             not recorded
        Reason codes                    none recorded

    The UI was right. `decisions.outcome` is the final answer; `decision_events`
    is the audit trail of the run that produced it, and the seed created the
    first without the second. So the screen the client is shown to demonstrate
    Reg B / ECOA evidence had none to show.

WHY IT COULD NOT SIMPLY BE FILLED IN
    The seeded outcome was not produced by the model. `db/init/003_seed_bulk.sql`
    chose it by rotating a literal array on the application id:

        (ARRAY['approve','approve','approve','deny','refer'])[1 + ((g * 2) % 5)]

    Run the deterministic stub scorer over the seeded income and SSN and it
    reproduced only 135 of the 306 stored outcomes. For the other 171 the
    model's own answer contradicted the stored one -- and every one of the six
    hand-curated anchor applications the demo actually points at was in that
    group. Writing an event that says "the model decided approve" beside a score
    of 602, which this system's own bands call `refer`, would fabricate exactly
    the audit record the table exists to hold.

    So the seed's INPUTS were corrected instead of its outputs invented. The
    outcome each application carries is unchanged; what changed is that the
    income and the SSN-derived bureau tier now land in the band that outcome
    names. Nothing about the product changed -- this is synthetic data made
    self-consistent.

    The correlation the seed already had is preserved: `applications.status` and
    `decisions.outcome` are both indexed by `(g * 2) % 5`, so funded applications
    are approved, decided ones denied and submitted ones referred. A model-driven
    outcome would have broken that and produced funded loans whose decision was a
    denial, which is a worse incoherence than the one being fixed.

    Bands, from `decision.py`, with `_stub_model_score(bureau, income) =
    int(bureau * 0.9 + income / 1000)` and `_stub_score(ssn) = 680 if the last
    SSN digit is even else 612`:

        approve   score >= 660   ->  bureau 680 and income >= 48_000
        refer     600..659       ->  bureau 680 and income  < 48_000
        deny      score  < 600   ->  bureau 612 and income  < 49_200

DETERMINISM AND WHY THE FORMULAS ARE REPRODUCED HERE
    Same reasoning as `regenerate_seed_offers.py`: the bulk seed generates its
    rows arithmetically in SQL, and those formulas are reproduced below rather
    than queried, so this tool needs no database and cannot be run against a
    drifted one by accident. If the seed formulas change, `_seeded_inputs()` must
    change with them -- which is why the generated block is CHECKED, not merely
    written. `db/tests/test_seed_decision_event_consistency.py` fails CI on a
    mismatch between this tool and the committed SQL.

NOTHING HERE IS A MODEL CALL
    The score, the reason codes and the feature attributions come from
    `decision-service`'s own shipped functions -- `_stub_model_score`,
    `_reason_codes`, `_stub_score` -- imported, not reimplemented. No network
    call is made and no vendor is contacted. `model_version` is the stub's own
    `f"{AI_MODEL_VERSION}-stub"`, which is the honest label: these rows record
    what the deterministic development scorer produces, and a row claiming a
    licensed vendor version would be the same fabrication in a different place.

    `top_features` is populated for the same reason `decision.py` populates it
    only for the stub: the bureau/income attribution IS the stub's scoring math,
    and is authoritative for it. A real vendor response carries no attributions,
    which is why the code records null there and why these rows must never be
    taken as evidence about a licensed model.

REFUSAL
    If the computed decision for any application disagrees with that
    application's stored `decisions.outcome`, this tool raises and writes
    nothing. It cannot emit an event that contradicts the outcome it claims to
    explain, which is the one failure that would make the seeded evidence worse
    than no evidence.
"""
import argparse
import datetime as dt
import json
import pathlib
import re
import sys

REPO = pathlib.Path(__file__).resolve().parents[2]
DECISION_SERVICE = REPO / "services" / "decision-service"
sys.path.insert(0, str(DECISION_SERVICE))

from app import decision as shipped  # noqa: E402  -- path set above

SEED_BULK = REPO / "db" / "init" / "003_seed_bulk.sql"
SEED_ANCHOR = REPO / "db" / "init" / "002_seed.sql"
EVENTS_SQL = REPO / "db" / "init" / "008_seed_decision_events.sql"

BEGIN = "-- BEGIN GENERATED DECISION EVENT ROWS (db/tools/regenerate_seed_decision_events.py)"
END = "-- END GENERATED DECISION EVENT ROWS"

#: The anchor applications in `002_seed.sql` are hand-written rather than
#: generated, so their inputs are PARSED OUT OF THE SEED rather than restated
#: here.
#:
#: They were restated here, in a literal table, and it was wrong within an hour:
#: application 6013's amount was typed as 7500 while the seed says 8000, so its
#: generated event recorded a requested amount no application had. Codex caught
#: it. The guard that was supposed to prevent exactly that --
#: `_assert_anchors_match_the_seed` -- only checked the SSN and the decisions
#: row, so it passed over the amount, the term and the income.
#:
#: Restating a value the file next to you already holds is the defect, not the
#: typo. Parsing removes the class: a hand edit to any anchor's amount, term or
#: income now reaches the generated events on the next `--write`, and the drift
#: check fails until it does.
#:
#: Three SSNs and one income were deliberately CHANGED in the seed so each
#: recorded bureau tier matches its recorded outcome. Every OUTCOME is untouched.

_APPLICATIONS_INSERT = re.compile(
    r"INSERT INTO applications\s*\(([^)]*)\)\s*VALUES\s*(?P<rows>.*?);", re.S)
_APPLICANTS_INSERT = re.compile(
    r"INSERT INTO applicants\s*\(([^)]*)\)\s*VALUES\s*(?P<rows>.*?);", re.S)
_DECISIONS_INSERT = re.compile(
    r"INSERT INTO decisions\s*\(([^)]*)\)\s*VALUES\s*(?P<rows>.*?);", re.S)


def _split_row(row: str):
    """Split one VALUES tuple on commas that are not inside a quoted literal."""
    out, buf, quoted = [], [], False
    for ch in row:
        if ch == "'":
            quoted = not quoted
            buf.append(ch)
        elif ch == "," and not quoted:
            out.append("".join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    out.append("".join(buf).strip())
    return out


def _rows_by_name(text: str, pattern):
    """Every VALUES tuple of one INSERT, as `{column: literal}`."""
    m = pattern.search(text)
    assert m, "expected an INSERT this tool can read in db/init/002_seed.sql"
    columns = [c.strip() for c in m.group(1).split(",")]
    rows = []
    depth, buf = 0, []
    quoted = False
    for ch in m.group("rows"):
        if ch == "'":
            quoted = not quoted
        if not quoted and ch == "(":
            depth += 1
            if depth == 1:
                buf = []
                continue
        if not quoted and ch == ")":
            depth -= 1
            if depth == 0:
                parts = _split_row("".join(buf))
                if len(parts) == len(columns):
                    rows.append(dict(zip(columns, parts)))
                continue
        if depth == 1:
            buf.append(ch)
    return rows


def _lit(value: str):
    v = value.strip()
    if v.upper() == "NULL":
        return None
    if v.startswith("'"):
        return v[1:-1].replace("''", "'")
    return v


def _anchor_inputs():
    """The six curated applications, read from the seed rather than restated."""
    text = SEED_ANCHOR.read_text(encoding="utf-8")

    ssn_by_applicant = {}
    for row in _rows_by_name(text, _APPLICANTS_INSERT):
        ssn_by_applicant[int(row["id"])] = _lit(row["ssn"]) or ""

    outcome_by_app = {}
    for row in _rows_by_name(text, _DECISIONS_INSERT):
        outcome_by_app[int(row["app_id"])] = _lit(row["outcome"])

    anchors = []
    for row in _rows_by_name(text, _APPLICATIONS_INSERT):
        app_id = int(row["id"])
        applicant = int(row["applicant_id"])
        assert applicant in ssn_by_applicant, (
            "application %d references applicant %d, which 002_seed.sql does "
            "not create" % (app_id, applicant))
        assert app_id in outcome_by_app, (
            "anchor application %d has no seeded decision, so no event can "
            "explain one" % app_id)
        anchors.append((
            app_id,
            ssn_by_applicant[applicant],
            float(row["income"]),
            float(row["amount"]),
            int(row["term_months"]),
            outcome_by_app[app_id],
        ))
    assert anchors, "no anchor applications parsed out of db/init/002_seed.sql"
    return tuple(anchors)


BULK_FIRST, BULK_LAST = 7000, 7299

#: `occurred_at` must be deterministic: a `now()` default would make the
#: generated block differ on every run and the drift check meaningless. Anchored
#: to a fixed date, spread by id so the ordering a screen sorts by is stable.
EPOCH = dt.datetime(2026, 6, 1, 9, 0, 0, tzinfo=dt.timezone.utc)


def _intended_outcome(app_id: int) -> str:
    """The seed's own rotation, reproduced. See the module docstring."""
    return ("approve", "approve", "approve", "deny", "refer")[(app_id * 2) % 5]


def _bulk_income(app_id: int, outcome: str) -> float:
    """An income inside the band the seeded outcome names.

    Keeps the seed's `(g * 311) % ...` shape so the spread stays wide and the
    values stay obviously synthetic-arithmetic rather than hand-picked.
    """
    spread = (app_id * 311)
    if outcome == "approve":
        return float(48_000 + (spread % 150_000))
    if outcome == "refer":
        return float(24_000 + (spread % 23_000))
    return float(24_000 + (spread % 25_000))          # deny


def _bulk_ssn(app_id: int, outcome: str) -> str:
    """The seed's SSN formula, with the LAST DIGIT forced to the needed tier.

    `_stub_score` reads only that digit, so this is the smallest change that
    makes the recorded bureau score consistent with the recorded outcome. The
    first eight digits keep the seed's own arithmetic.
    """
    applicant = 100 + (app_id - BULK_FIRST)
    area = "%03d" % ((applicant * 131) % 900 + 100)
    group = "%02d" % ((applicant * 17) % 90 + 10)
    serial = (applicant * 53) % 9000 + 1000
    # 680 for approve and refer, 612 for deny.
    want_even = outcome != "deny"
    if (serial % 2 == 0) != want_even:
        serial += 1 if serial % 10 != 9 else -1
    return "%s-%s-%04d" % (area, group, serial)


def _bulk_amount(app_id: int) -> float:
    return float(1_000 + ((app_id * 263) % 49_000))


def _bulk_term(app_id: int) -> int:
    return (12, 24, 36, 48, 60)[(app_id * 3) % 5]


def _seeded_inputs():
    """Every application with a decision, and the inputs the model saw."""
    rows = list(_anchor_inputs())
    for app_id in range(BULK_FIRST, BULK_LAST + 1):
        outcome = _intended_outcome(app_id)
        rows.append((app_id, _bulk_ssn(app_id, outcome),
                     _bulk_income(app_id, outcome), _bulk_amount(app_id),
                     _bulk_term(app_id), outcome))
    return rows


def _band(score: int) -> str:
    """`_run_model`'s own thresholds, in one place so a reader can check them."""
    if score >= 660:
        return "approve"
    return "deny" if score < 600 else "refer"


def _event(app_id, ssn, income, amount, term, outcome):
    bureau = shipped._stub_score(ssn)
    score = shipped._stub_model_score(bureau, income)
    decided = _band(score)
    if decided != outcome:
        raise SystemExit(
            "REFUSING TO WRITE. Application %d carries outcome %r, but its "
            "seeded inputs (bureau %d, income %.0f) score %d, which this "
            "system's bands call %r. Writing an event here would fabricate the "
            "audit record it claims to be. Fix the seeded INPUTS -- never the "
            "event -- so the two agree."
            % (app_id, outcome, bureau, income, score, decided))

    # Exactly what `_run_model` returns for a stub response: no reason codes on
    # an approval (there is no adverse action to explain), the shipped
    # attribution for everything else.
    reasons = [] if decided == "approve" else shipped._reason_codes(bureau, income)
    features = {
        "bureau_score": bureau,
        "income": income,
        "bureau_contribution": round(bureau * 0.9, 2),
        "income_contribution": round(income / 1000, 2),
    }
    occurred = EPOCH + dt.timedelta(minutes=(app_id % 4000))
    return {
        "app_id": app_id,
        "occurred_at": occurred.strftime("%Y-%m-%d %H:%M:%S+00"),
        "requested_amount": amount,
        "term_months": term,
        "annual_income": income,
        "bureau_score": bureau,
        "model_score": score,
        "model_version": "%s-stub" % shipped.AI_MODEL_VERSION,
        "top_features": features,
        "decision": decided,
        "reason_codes": reasons,
    }


def _sql_literal(value):
    if isinstance(value, (dict, list)):
        return "'%s'::jsonb" % json.dumps(value, sort_keys=True).replace("'", "''")
    if isinstance(value, str):
        return "'%s'" % value.replace("'", "''")
    if isinstance(value, float):
        return "%.2f" % value
    return str(value)


COLUMNS = ("app_id", "occurred_at", "requested_amount", "term_months",
           "annual_income", "bureau_score", "model_score", "model_version",
           "top_features", "decision", "reason_codes")


def generated_block() -> str:
    events = [_event(*row) for row in _seeded_inputs()]
    lines = [BEGIN,
             "INSERT INTO decision_events (%s) VALUES" % ", ".join(COLUMNS)]
    rendered = []
    for e in events:
        rendered.append("  (%s)" % ", ".join(_sql_literal(e[c]) for c in COLUMNS))
    lines.append(",\n".join(rendered) + ";")
    lines.append("SELECT setval('decision_events_id_seq', %d);" % len(events))
    lines.append(END)
    return "\n".join(lines)


def _assert_anchors_match_the_seed():
    """Guard-the-guard for the half of the input set that is not derivable.

    The anchors are now PARSED, so drift between this tool and the seed is not
    possible in the way it was. What is still worth asserting is that the parse
    found the whole set and something real: a regex that silently matched
    nothing would make every anchor event disappear, and the bulk rows would
    still generate, so the run would look successful.
    """
    anchors = _anchor_inputs()
    assert len(anchors) >= 6, (
        "parsed only %d anchor applications out of db/init/002_seed.sql; the "
        "INSERT shape has changed and this tool must fail rather than emit a "
        "partial seed" % len(anchors))
    text = SEED_ANCHOR.read_text(encoding="utf-8")
    for app_id, ssn, income, amount, term, outcome in anchors:
        assert outcome in ("approve", "deny", "refer"), (
            "application %d has outcome %r" % (app_id, outcome))
        assert amount > 0 and term > 0, (
            "application %d parsed as amount=%s term=%s -- the column order in "
            "002_seed.sql has moved" % (app_id, amount, term))
        if ssn:
            assert ssn in text, (
                "parsed SSN %s for application %d is not in the seed" % (ssn, app_id))


HEADER = """-- Seeded automated decision evidence.
--
-- GENERATED. Do not edit by hand: run
--     python db/tools/regenerate_seed_decision_events.py --write
-- and commit the result. `db/tests/test_seed_decision_event_consistency.py`
-- fails on drift between this file and that tool.
--
-- One row per seeded application, recording what decision-service's own
-- deterministic development scorer produces from that application's seeded
-- income and SSN-derived bureau score. The tool's docstring carries the whole
-- rationale, including why the seeded INPUTS were corrected rather than these
-- outputs invented, and why `model_version` says `-stub`.
--
-- Applies after 002_seed.sql and 003_seed_bulk.sql because every row references
-- an application those files create, and after 004_decision_events.sql, which
-- creates the table and its append-only trigger. That trigger is why this file
-- only ever INSERTs: a corrected seed is a new database, not an UPDATE.
"""


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--write", action="store_true",
                        help="rewrite db/init/008_seed_decision_events.sql")
    args = parser.parse_args()

    _assert_anchors_match_the_seed()
    block = generated_block()
    wanted = HEADER + "\n" + block + "\n"

    counts = {}
    for row in _seeded_inputs():
        counts[row[5]] = counts.get(row[5], 0) + 1
    print("decision events: %d (%s)"
          % (sum(counts.values()),
             ", ".join("%s %d" % kv for kv in sorted(counts.items()))))

    if args.write:
        EVENTS_SQL.write_text(wanted, encoding="utf-8")
        print("wrote %s" % EVENTS_SQL.relative_to(REPO))
        return 0

    if not EVENTS_SQL.is_file():
        print("MISSING: %s -- run with --write" % EVENTS_SQL.relative_to(REPO))
        return 1
    if EVENTS_SQL.read_text(encoding="utf-8") != wanted:
        print("DRIFT: %s does not match this tool's output -- run with --write"
              % EVENTS_SQL.relative_to(REPO))
        return 1
    print("up to date")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
