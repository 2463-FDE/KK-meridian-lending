"""Every published benchmark number must come from the committed artifact.

Review of PR #12 found the two evidence files were from different runs:
`results.json` reported build times of 0.139/1.210 s and included a path
benchmark, while `run-output.txt` reported 0.238/2.317 s, different traversal
timings, and no path section at all -- even though its own footer said it had
written that JSON. Two files from two runs provide provenance for neither, and
the ADR's numbers could not be traced to either one.

Three things are asserted here, and they are deliberately mechanical rather than
a reviewer's diff:

  1. the two artifacts are from the SAME invocation (`run_id` appears in both);
  2. every number in adr/0009's tables matches `results.json`;
  3. docs/ROADMAP.md's traversal claim matches it too.

No Postgres needed: this reads committed files. `--transcript` makes producing
both artifacts one command, and this makes forgetting to regenerate one of them a
test failure instead of a review finding.
"""
import json
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
BENCH = REPO / "db" / "bench"
RESULTS = BENCH / "results.json"
TRANSCRIPT = BENCH / "run-output.txt"
ADR = REPO / "adr" / "0009-graph-store-for-identity-traversal.md"
ROADMAP = REPO / "docs" / "ROADMAP.md"


@pytest.fixture(scope="module")
def results():
    assert RESULTS.is_file(), f"{RESULTS} is missing -- the ADR has no evidence"
    return json.loads(RESULTS.read_text(encoding="utf-8"))


def _num(text):
    """'1,621' -> 1621, '0.108' -> 0.108, '**0.318 s**' -> 0.318."""
    cleaned = re.sub(r"[^\d.]", "", text.replace(",", ""))
    if not cleaned:
        return None
    return float(cleaned) if "." in cleaned else int(cleaned)


def _table_rows(markdown, header_contains):
    """The data rows of the first pipe table whose header matches."""
    rows, in_table = [], False
    for line in markdown.splitlines():
        stripped = line.strip()
        if not stripped.startswith("|"):
            in_table = False
            continue
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if not in_table:
            if all(k in stripped for k in header_contains):
                in_table = True
            continue
        if set("".join(cells)) <= set("-: "):
            continue                      # the separator row
        rows.append(cells)
    return rows


def test_the_two_artifacts_are_from_the_same_run(results):
    """The finding, as a test. A shared run_id or neither file is evidence."""
    run_id = results.get("run_id")
    assert run_id, (
        "results.json has no run_id -- regenerate it with the current benchmark, "
        "which stamps one into both artifacts"
    )
    assert TRANSCRIPT.is_file(), f"{TRANSCRIPT} is missing"
    transcript = TRANSCRIPT.read_text(encoding="utf-8", errors="replace")
    assert run_id in transcript, (
        f"run_id {run_id!r} from results.json does not appear in run-output.txt, "
        f"so the two artifacts are from different runs. Regenerate both in one "
        f"invocation: --json results.json --transcript run-output.txt"
    )


def test_the_transcript_contains_the_path_phase(results):
    """The transcript must cover what the JSON reports.

    The old pair failed exactly here: the JSON had a path benchmark and the
    transcript had never run one.
    """
    transcript = TRANSCRIPT.read_text(encoding="utf-8", errors="replace")
    assert "unbounded WITH the connecting path" in transcript, (
        "the transcript has no path phase, but results.json reports one"
    )
    assert "unbounded_with_path" in json.dumps(results)


def test_the_adr_depth_table_matches_the_artifact(results):
    """Reached counts and per-candidate timings, cell by cell."""
    rows = _table_rows(ADR.read_text(encoding="utf-8"),
                       ("Depth", "Reached", "frontier-attr"))
    assert rows, "adr/0009 has no depth table -- or its header changed"

    for cells in rows:
        depth = str(_num(cells[0]))
        slot = results["candidates"]["frontier-attr"]["depths"].get(depth)
        assert slot, f"the ADR quotes depth {depth}, which this run does not contain"
        assert _num(cells[1]) == slot["reached"], (
            f"depth {depth}: ADR says {cells[1]} reached, artifact says "
            f"{slot['reached']}"
        )
        for offset, candidate in enumerate(
            ("frontier-attr", "materialized", "global-edge"), start=2
        ):
            published = _num(cells[offset])
            measured = results["candidates"][candidate]["depths"][depth]["seconds"]
            assert published == pytest.approx(measured, abs=0.0005), (
                f"depth {depth} {candidate}: ADR says {published}s, artifact "
                f"says {measured}s -- transcribe from the artifact, do not round"
            )


def test_the_adr_build_table_matches_the_artifact(results):
    rows = _table_rows(ADR.read_text(encoding="utf-8"), ("Structure", "Build", "Rows"))
    assert len(rows) >= 2, "adr/0009 has no build table"
    published = {r[0]: (_num(r[1]), _num(r[2])) for r in rows}

    attr = next(v for k, v in published.items() if "identity_attr" in k)
    edges = next(v for k, v in published.items() if "edges" in k)
    assert attr == (results["build"]["identity_attr_seconds"],
                    results["build"]["identity_attr_rows"]), attr
    assert edges == (results["build"]["edges_seconds"],
                     results["build"]["edge_rows"]), edges


def test_the_adr_unbounded_and_path_numbers_match_the_artifact(results):
    text = ADR.read_text(encoding="utf-8")
    unbounded = results["unbounded"]
    path = results["unbounded_with_path"]

    rows = _table_rows(text, ("Traversal", "frontier-attr", "Reached"))
    assert rows, "adr/0009 has no unbounded table"
    cells = rows[0]
    assert _num(cells[1]) == pytest.approx(unbounded["frontier-attr"]["seconds"], abs=0.0005)
    assert _num(cells[2]) == pytest.approx(unbounded["materialized"]["seconds"], abs=0.0005)
    assert _num(cells[3]) == unbounded["frontier-attr"]["reached"]

    phases = _table_rows(text, ("Phase", "Time", "Result"))
    assert len(phases) >= 3, "adr/0009 has no path-phase table"
    published = {r[0].lower(): _num(r[1]) for r in phases}
    walk = next(v for k, v in published.items() if "walk" in k)
    recon = next(v for k, v in published.items() if "reconstruct" in k)
    total = next(v for k, v in published.items() if "total" in k)
    assert walk == pytest.approx(path["walk_seconds"], abs=0.0005)
    assert recon == pytest.approx(path["path_reconstruction_seconds"], abs=0.0005)
    assert total == pytest.approx(path["total_seconds"], abs=0.0005)


def test_the_roadmap_traversal_claim_matches_the_artifact(results):
    """ROADMAP quoted its own numbers once and drifted from the ADR."""
    text = ROADMAP.read_text(encoding="utf-8")
    # Only the lines that CITE this benchmark. A first version sliced the file
    # from its first mention of "graph" to the end and then failed on a 2.4-second
    # payment latency three hundred lines later -- an unrelated number, correctly
    # quoted, in a document this test has no business policing. The scope is the
    # claim, not the file.
    claim_lines = [
        line for line in text.splitlines()
        if "graph_traversal_benchmark" in line
        or "results.json" in line
        or "run-output.txt" in line
    ]
    if not claim_lines:
        pytest.skip("docs/ROADMAP.md does not cite the graph benchmark")
    section = "\n".join(claim_lines)

    allowed = {
        results["unbounded_with_path"]["total_seconds"],
        results["unbounded_with_path"]["walk_seconds"],
        results["unbounded_with_path"]["path_reconstruction_seconds"],
        results["unbounded"]["frontier-attr"]["seconds"],
        results["unbounded"]["materialized"]["seconds"],
        results["build"]["identity_attr_seconds"],
        results["build"]["edges_seconds"],
    }
    allowed |= {
        v["seconds"]
        for cand in results["candidates"].values()
        for v in cand["depths"].values()
        if v.get("seconds") is not None
    }
    # A retracted number may be quoted as history -- that is how a reader avoids
    # re-deriving a figure this project already disproved, and the roadmap does
    # exactly that with the two earlier benchmark defects ("16.9-38.7s at depth
    # 4", "72.3s at depth 4"). Only PRESENT-TENSE claims have to match the
    # artifact, so a figure whose own clause marks it as former is skipped. The
    # cue must sit in the SAME clause, for the same reason as the documentation
    # guard in payment-service: a retraction three sentences away is not a
    # retraction of this number.
    retracted = re.compile(
        r"\b(?:earlier|previously|former|used to|was|were|no longer|"
        r"defects?|first|then|retracted|old)\b", re.IGNORECASE
    )
    for clause in re.split(r"[;.]\s+|\s+—\s+|\s+--\s+", section):
        for raw in re.findall(r"(\d+\.\d+)\s*s(?:econds)?\b", clause):
            value = float(raw)
            if any(abs(value - a) < 0.0005 for a in allowed):
                continue
            if retracted.search(clause):
                continue                  # quoted as history, not as a claim
            pytest.fail(
                f"docs/ROADMAP.md states {value}s as a current benchmark figure, "
                f"and it is not in db/bench/results.json ({sorted(allowed)}). "
                f"Transcribe from the artifact, or mark the number as historical.\n"
                f"clause: {clause.strip()[:200]}"
            )


def test_the_plan_parser_reads_measured_rows_not_the_planner_estimate():
    """`EXPLAIN (ANALYZE)` prints both, and only one of them is evidence.

    A first version matched the first `rows=` on the line, which is the planner's
    ESTIMATE -- so results.json, the transcript and the ADR published 609,883 as a
    measured figure while the plan beside it said 553,928. Publishing an estimate
    as a measurement is the failure this whole benchmark exists to stop, and it
    was doing it inside the note about honesty. Reviewed on PR #12.

    Also asserts the pattern is anchored to `edge_rel`: unanchored, it attached a
    MATERIALIZED rescan note to the ordinary `walk` CTE in the other two
    candidates, which are not materialized and rebuild nothing.
    """
    import importlib.util

    tool = BENCH / "graph_traversal_benchmark.py"
    spec = importlib.util.spec_from_file_location("graph_bench", tool)
    bench = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bench)

    edge_line = (
        "  ->  CTE Scan on edge_rel e  (cost=0.00..12197.66 rows=609883 width=8) "
        "(actual time=136.043..243.703 rows=553928 loops=5)"
    )
    parsed = bench._cte_rescans([edge_line])
    assert parsed == {"relation": "edge_rel", "rows_per_scan": 553928, "scans": 5}, parsed
    assert parsed["rows_per_scan"] != 609883, "the planner estimate was published as measured"

    walk_line = (
        "  ->  CTE Scan on walk  (cost=0.00..1829.62 rows=91481 width=4) "
        "(actual time=63.817..1617.762 rows=5936 loops=1)"
    )
    assert bench._cte_rescans([walk_line]) == {}, (
        "a non-materialized CTE was reported as a materialized rescan"
    )

    # The invisible failure this repository has now hit three times: a `\b`
    # written through a shell becomes a literal U+0008, the pattern still
    # compiles, and it silently matches nothing.
    assert "\x08" not in bench._CTE_SCAN_RE.pattern, (
        "the plan pattern contains a control character -- it will match nothing "
        "and every rescan note will silently disappear"
    )


def test_the_published_rescan_figure_is_the_one_in_the_committed_plan(results):
    """results.json must agree with the plan it was parsed from."""
    entry = results["explain"].get("global-edge", {}).get("cte_rescans")
    assert entry, "results.json has no cte_rescans for global-edge"
    plan = "\n".join(results["explain"]["global-edge"]["plan"])
    assert "CTE Scan on edge_rel" in plan, "the committed plan has no edge_rel scan"
    assert re.search(
        r"CTE Scan on edge_rel\b.*?\(actual [^)]*?rows=%d loops=%d\)"
        % (entry["rows_per_scan"], entry["scans"]),
        plan,
    ), (
        "the published rescan figures (%s) do not appear as measured values in the "
        "committed plan" % entry
    )


# --- prose, not just table cells ---------------------------------------------
#
# Reviewed on PR #12: the tables were transcribed from the regenerated run while
# the surrounding NARRATIVE still quoted the previous one, and
# `services/origination-service/app/kg.py` repeated the old figures in its module
# docstring. A reader got contradictory evidence from the same page. Table-cell
# checking could not see any of it, so the scan is widened to every second-shaped
# figure in the documents that cite this benchmark.

PROSE_SOURCES = [
    ADR,
    REPO / "services" / "origination-service" / "app" / "kg.py",
]

_SECONDS = re.compile(r"(\d+\.\d+)\s*(?:s\b|seconds\b|s\*\*)", re.IGNORECASE)
# A figure may be quoted as history if its own clause says so -- the same
# same-clause rule the logging guard uses. Without this, recording "an earlier
# run reported 0.108s" would be forbidden, which would push authors to delete the
# history rather than mark it.
_HISTORICAL = re.compile(
    r"\b(?:earlier|previously|former|used to|was|were|no longer|old|first|then|"
    r"before|retracted|superseded|stale|drifted|had claimed)\b", re.IGNORECASE
)


def _artifact_figures(results):
    values = {
        results["build"]["identity_attr_seconds"],
        results["build"]["edges_seconds"],
        results["unbounded_with_path"]["walk_seconds"],
        results["unbounded_with_path"]["path_reconstruction_seconds"],
        results["unbounded_with_path"]["total_seconds"],
    }
    values |= {v["seconds"] for v in results["unbounded"].values() if v.get("seconds")}
    values |= {
        v["seconds"]
        for cand in results["candidates"].values()
        for v in cand["depths"].values()
        if v.get("seconds") is not None
    }
    return values


@pytest.mark.parametrize("source", PROSE_SOURCES, ids=lambda p: p.name)
def test_prose_quotes_no_timing_absent_from_the_artifact(results, source):
    """Every second-shaped figure in the narrative must be from this run."""
    assert source.is_file(), f"{source} is missing"
    allowed = _artifact_figures(results)
    text = source.read_text(encoding="utf-8")

    offenders = []
    for clause in re.split(r"(?<=[.;])\s+|\n\n", text):
        for raw in _SECONDS.findall(clause):
            value = float(raw)
            if any(abs(value - a) < 0.0005 for a in allowed):
                continue
            if _HISTORICAL.search(clause):
                continue
            offenders.append("%s -> %s" % (value, " ".join(clause.split())[:150]))

    assert not offenders, (
        "%s quotes timings that are not in db/bench/results.json:\n  %s\n\n"
        "Transcribe from the artifact, or mark the figure as historical in the "
        "same clause." % (source.name, "\n  ".join(offenders))
    )


@pytest.mark.parametrize("source", PROSE_SOURCES, ids=lambda p: p.name)
def test_prose_names_the_run_it_quotes(results, source):
    """A document quoting these numbers must name the run they came from.

    `kg.py` cited a run id that no longer existed anywhere, which is how its
    figures drifted unnoticed: nothing tied them to an artifact.
    """
    text = source.read_text(encoding="utf-8")
    if not _SECONDS.search(text):
        pytest.skip(f"{source.name} quotes no timings")
    assert results["run_id"] in text, (
        f"{source.name} quotes timings but does not name run_id "
        f"{results['run_id']} -- a reader cannot tell which run governs"
    )
