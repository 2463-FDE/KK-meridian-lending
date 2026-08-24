"""No runtime path may infer, group by, or carry protected-class data.

**Authority.** Charles Jester, client, 2026-08-24: there is no permission to
collect real protected-class data for this demonstration, there is **no approved
proxy**, and one may not be created "including from ZIP, ZIP3 or similar fields".
Synthetic protected-class labels are permitted **only** inside an isolated
offline evaluation fixture, and must never enter model inputs, runtime
application inputs, decisions, operational records, runtime database records,
traces, telemetry or consumer output.

That superseded Week 8's own design. `services/origination-service/app/fair_lending.py`
grouped recorded decisions by ZIP3 and applied the four-fifths rule at
`GET /applications/fair-lending/zip-analysis`; both are retired. This guard is
what stops them -- or a replacement proxy -- coming back.

**What this deliberately permits.** Documentation must be able to name the thing
it prohibits, so `.md` files, ADRs and specs are not scanned for the words. A
rule that made the prohibition unwriteable would be a worse rule. What is scanned
is runtime service code: the request schemas, the decision path, the persisted
rows, the traces and the consumer-facing output.

**Scope limit, stated rather than implied.** This checks that no *code path* in
this repository infers or carries a protected class. It cannot prove the absence
of protected-class inference in general -- a sufficiently indirect feature could
correlate with one, which is a modelling question no static check answers, and
which spec 0003 records as the reason no fairness claim may be made.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SERVICES = REPO / "services"

#: The single isolated location the client's synthetic labels may occupy. Nothing
#: under it is a runtime path, and nothing outside it may carry a label.
OFFLINE_FIXTURE_DIR = REPO / "fixtures" / "offline_fairness_training"

#: Protected-class vocabulary, matched as a FIELD rather than as a word.
#:
#: A bare `\brace\b` was tried and is useless here: this codebase discusses
#: concurrency constantly, so it fired on "wins its finality race" in a
#: docstring. What matters is a protected class being carried as data -- a quoted
#: key, or an identifier being assigned or passed -- so that is what is matched.
_PROTECTED_WORDS = (
    r"protected_class|protected_group|race|ethnicity|ethnic_group|"
    r"national_origin|marital_status|religion|sex_code|gender_code|"
    r"age_band|disability_status")
PROTECTED_FIELDS = re.compile(
    rf"""["'`](?:{_PROTECTED_WORDS})["'`]"""      # a dict key or column name
    rf"""|\b(?:{_PROTECTED_WORDS})\s*[:=]"""      # an annotation, kwarg or assignment
    rf"""|\b(?:applicant_|customer_|borrower_)(?:{_PROTECTED_WORDS})\b""",
    re.I)

#: Proxy machinery: inferring a protected class from something else.
PROXY_MACHINERY = re.compile(
    r"\b(bisg|surname_proxy|geo_proxy|census_proxy|proxy_race|"
    r"race_proxy|demographic_inference|infer_protected|"
    r"zip3_disparate_impact|zip_disparate_impact)\b", re.I)

#: The retired screen's own shape, in two independent halves.
#:
#: 1. A ZIP-derived grouping key anywhere near a four-fifths threshold. The
#:    window spans newlines deliberately: a renamed module that sliced
#:    `zip_code[:3]` on one line and compared against `0.8` four lines later
#:    survived a single-line version of this check, which is exactly the
#:    "rename it and carry on" case the client's decision forbids.
#: 2. Truncating a ZIP at all. Nothing in this system has a legitimate reason to
#:    take the first three characters of a postal code -- that operation exists
#:    to build a geographic grouping key, which is the proxy itself.
FOUR_FIFTHS_ON_ZIP = re.compile(
    r"(zip3|zip_?3|zip_code\s*\[\s*:?\s*0?\s*:?\s*3\s*\])[\s\S]{0,400}"
    r"(four[_ -]?fifths|0\.8\b|80%)|"
    r"(four[_ -]?fifths|0\.8\b|80%)[\s\S]{0,400}(zip3|zip_?3)", re.I)

#: Tolerant of the punctuation between the name and the slice: the field is
#: usually reached as `row["zip_code"][:3]`, so the closing quote and bracket sit
#: in between. A pattern that demanded `zip_code[:3]` adjacency missed exactly
#: that form under mutation.
ZIP_TRUNCATION = re.compile(
    r"""zip(?:_code)?["'\]\)\s]{0,6}\[\s*0?\s*:\s*3\s*\]"""
    # SQL forms too. `substring(ap.zip_code from 1 for 3)` is the same
    # truncation with a different dialect, and the first version of this pattern
    # required the digit within 40 characters of the opening paren *and* an
    # unqualified column name -- both of which a real query breaks.
    r"""|substring\s*\(\s*[\w.]*zip[\w.]*[\s\S]{0,60}?\b3\b"""
    r"""|left\s*\(\s*[\w.]*zip[\w.]*[\s\S]{0,30}?\b3\b"""
    r"""|zip[\w.]*\s*~\s*'\^\\\\d\{3\}'""", re.I)


def _runtime_sources():
    """Every service source file that runs in a request or job path.

    Tests are excluded: a test may legitimately assert that a label never
    arrives, which means naming it.
    """
    for path in sorted(SERVICES.rglob("*.py")):
        parts = set(path.parts)
        if "tests" in parts or "__pycache__" in parts:
            continue
        yield path


def _frontend_sources():
    for pattern in ("*.ts", "*.tsx"):
        for path in sorted((REPO / "frontend").rglob(pattern)):
            parts = set(path.parts)
            if "node_modules" in parts or "e2e" in parts or ".next" in parts:
                continue
            yield path


# --------------------------------------------------------------------------
# The retired screen stays retired.
# --------------------------------------------------------------------------

def test_the_zip3_fairness_module_is_gone():
    module = SERVICES / "origination-service" / "app" / "fair_lending.py"
    assert not module.exists(), (
        "the ZIP3 fair-lending module is back. The client prohibited ZIP and "
        "ZIP3 as a protected-class proxy on 2026-08-24; a runtime screen built "
        "on it cannot be reinstated by re-adding the file")


def test_the_zip_analysis_route_is_not_registered():
    applications = (SERVICES / "origination-service" / "app" / "routers"
                    / "applications.py").read_text(encoding="utf-8")

    assert "fair-lending/zip-analysis" not in applications, (
        "the ZIP3 disparate-impact route is registered again")
    assert "fair_lending" not in applications, (
        "origination's router imports a fair_lending module again")


def test_no_runtime_source_groups_decisions_by_a_zip_derived_key():
    """Renaming the screen would not make it permitted, so the shape is checked
    rather than the name: a ZIP-derived grouping key next to a four-fifths
    threshold."""
    offenders = []
    for path in _runtime_sources():
        body = path.read_text(encoding="utf-8", errors="replace")
        match = FOUR_FIFTHS_ON_ZIP.search(body)
        if match:
            offenders.append(f"{path.relative_to(REPO)}: {match.group(0)[:120]}")

    assert not offenders, (
        "a runtime path applies a four-fifths-style test to a ZIP-derived "
        "group:\n" + "\n".join(offenders))


def test_no_runtime_source_truncates_a_zip_into_a_grouping_key():
    """The narrower, sharper check: taking the first three characters of a
    postal code has one purpose, and it is the one the client prohibited.

    Kept separate from the four-fifths check because a proxy does not need a
    threshold to be a proxy -- grouping decisions by ZIP3 and eyeballing the
    rates is the same substitution with the arithmetic moved into a human's
    head.
    """
    offenders = []
    for path in _runtime_sources():
        body = path.read_text(encoding="utf-8", errors="replace")
        match = ZIP_TRUNCATION.search(body)
        if match:
            line_no = body.count("\n", 0, match.start()) + 1
            offenders.append(
                f"{path.relative_to(REPO)}:{line_no}: {match.group(0)}")

    assert not offenders, (
        "a runtime path truncates a ZIP, which is how a geographic "
        "protected-class proxy is built:\n" + "\n".join(offenders))


@pytest.mark.parametrize("kind", ["backend", "frontend"])
def test_no_runtime_source_builds_a_protected_class_proxy(kind):
    sources = _runtime_sources() if kind == "backend" else _frontend_sources()
    offenders = []
    for path in sources:
        body = path.read_text(encoding="utf-8", errors="replace")
        match = PROXY_MACHINERY.search(body)
        if match:
            offenders.append(f"{path.relative_to(REPO)}: {match.group(0)}")

    assert not offenders, (
        "a runtime path names proxy machinery for protected-class inference. "
        "The client's 2026-08-24 decision forbids creating one, from ZIP or "
        "anything else:\n" + "\n".join(offenders))


# --------------------------------------------------------------------------
# No protected-class field reaches a runtime surface.
# --------------------------------------------------------------------------

def test_no_protected_class_field_appears_in_runtime_service_code():
    offenders = []
    for path in _runtime_sources():
        body = path.read_text(encoding="utf-8", errors="replace")
        for match in PROTECTED_FIELDS.finditer(body):
            line_no = body.count("\n", 0, match.start()) + 1
            line = body.splitlines()[line_no - 1].strip()
            # A comment explaining the prohibition is allowed to name it; a
            # field, column, key or parameter is not.
            if line.lstrip().startswith(("#", '"', "'", "*")):
                continue
            offenders.append(f"{path.relative_to(REPO)}:{line_no}: {line[:120]}")

    assert not offenders, (
        "protected-class vocabulary appears in runtime code outside a comment. "
        "It may exist only in the isolated offline fixture and in documentation "
        "explaining the prohibition:\n" + "\n".join(offenders))


def test_the_intake_schema_accepts_no_protected_class_field():
    """The request boundary is where a label would enter the system."""
    schemas = (SERVICES / "origination-service" / "app"
               / "schemas.py").read_text(encoding="utf-8")

    assert PROTECTED_FIELDS.search(schemas) is None, (
        "origination's request schema names a protected-class field, so one can "
        "be posted into the application record")


def test_the_decision_payload_carries_no_protected_class_field():
    """What the model is told, and what the audit row keeps."""
    decision = (SERVICES / "decision-service" / "app"
                / "decision.py").read_text(encoding="utf-8")
    schemas = (SERVICES / "decision-service" / "app"
               / "schemas.py").read_text(encoding="utf-8")

    for label, body in (("decision.py", decision), ("schemas.py", schemas)):
        for match in PROTECTED_FIELDS.finditer(body):
            line_no = body.count("\n", 0, match.start()) + 1
            line = body.splitlines()[line_no - 1].strip()
            if line.startswith(("#", '"', "'")):
                continue
            pytest.fail(
                f"decision-service/{label}:{line_no} names a protected-class "
                f"field outside a comment: {line[:160]}")

    # And the persisted audit row's columns are fixed by the schema, so the
    # absence is checked there too rather than inferred from the writer.
    events = (REPO / "db" / "init" / "004_decision_events.sql").read_text(
        encoding="utf-8")
    assert PROTECTED_FIELDS.search(events) is None, (
        "decision_events has a protected-class column")


def test_no_zip_is_sent_to_the_model_or_the_bureau():
    """ZIP is not a protected class, but it is the field the retired screen used
    as a proxy -- so the model must not receive it either, and the model card
    says so."""
    decision = (SERVICES / "decision-service" / "app"
                / "decision.py").read_text(encoding="utf-8")

    assert "zip" not in decision.lower(), (
        "decision-service's scoring path mentions ZIP; the model is told the "
        "applicant's amount, term, income and bureau score, and adding a "
        "geographic field would build the proxy the client prohibited")


# --------------------------------------------------------------------------
# The offline fixture boundary.
# --------------------------------------------------------------------------

def test_the_offline_fixture_location_is_isolated_and_labelled():
    """The client's package is not in the repository yet.

    Until it is, the location exists with its rules written down and nothing
    else -- no invented labels, no invented taxonomy, no evaluator reading a
    file that does not exist. `docs/DEBT.md` carries the dependency.
    """
    readme = OFFLINE_FIXTURE_DIR / "README.md"
    assert readme.is_file(), (
        f"{readme.relative_to(REPO)} is missing: the isolated location for the "
        f"client's synthetic fixture, and the statement of what may live there")

    text = readme.read_text(encoding="utf-8")
    for required in ("SYNTHETIC", "TRAINING ONLY", "NOT VENDOR ISSUED",
                     "NOT PRODUCTION EVIDENCE"):
        assert required in text, (
            f"the fixture location's README does not carry the {required!r} "
            f"label the client required")

    assert "CLIENT-PROVIDED-FIXTURE-NOT-PRESENT" in text, (
        "the README does not record that the package has not been supplied")


def test_no_runtime_code_reads_the_offline_fixture_location():
    """Structural, not procedural: if no runtime module can even name the
    directory, an offline label cannot become a runtime input by accident."""
    offenders = []
    for path in _runtime_sources():
        body = path.read_text(encoding="utf-8", errors="replace")
        if "offline_fairness_training" in body:
            offenders.append(str(path.relative_to(REPO)))

    assert not offenders, (
        "runtime service code references the offline fairness fixture "
        "directory:\n" + "\n".join(offenders))


def test_the_offline_location_holds_no_fabricated_data():
    """The client supplies the package. This repository must not invent it --
    a fabricated fixture would be exactly the "synthetic package masquerading
    as vendor material" the client warned against, one step earlier."""
    if not OFFLINE_FIXTURE_DIR.is_dir():
        pytest.skip("the fixture location does not exist yet")

    unexpected = [p.name for p in OFFLINE_FIXTURE_DIR.iterdir()
                  if p.name not in {"README.md"}]
    if unexpected:
        # Once the client's package lands this test's job changes: it should then
        # assert the package's own labelling. Failing loudly is the right
        # behaviour in between.
        pytest.fail(
            "files appeared in the offline fixture location: "
            f"{unexpected}. If this is the client's supplied package, update "
            "this test to assert its labelling and update docs/DEBT.md's "
            "CLIENT-PROVIDED-FIXTURE-NOT-PRESENT entry; if it was generated "
            "here, delete it -- this repository does not author the fixture")
