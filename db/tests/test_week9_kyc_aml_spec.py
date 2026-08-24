"""Guards on spec 0004 and ADR 0012 — the claims that would make them dangerous.

Week 9's deliverable is a specification, and the client's ask was explicit about
what it wanted instead: *"just make it look thorough for the launch"*. So the
failure mode for these two documents is not going stale. It is a document that
quietly supplies an answer nobody with the authority gave — a match threshold, a
disposition for a sanctions hit, a SAR trigger, an escalation owner — because
each of those reads as diligence and none of them is this repository's to decide.
That is the same defect the maker-checker limits were before they were approved
(`docs/DEBT.md` D8), with a worse consequence attached.

The second failure mode is the opposite one: a document that describes the
screening as though it exists. `sanctions_screened` is still a hardcoded `False`
in `kyc-service/app/schemas.py`, `kyc_checks` still has no column for it, and
D11 is still open. A spec that reads as an implementation report would make the
gap invisible, which is the only thing keeping it honest right now.

So this checks: no invented policy, no claimed implementation, every unavailable
authority carries a label, and the parts that ARE decided (fail-closed,
idempotency, provenance, CIP-is-not-CDD) are actually stated. Prose is not
frozen — what is asserted is the presence of a labelled decision, or the absence
of a fabricated one.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
SPEC = REPO / "specs" / "0004-kyc-aml-ubo-and-sanctions-screening.md"
ADR = REPO / "adr" / "0012-sanctions-screening-integration.md"
KYC_SCHEMAS = REPO / "services" / "kyc-service" / "app" / "schemas.py"
KYC = REPO / "services" / "kyc-service" / "app" / "kyc.py"
DEBT = REPO / "docs" / "DEBT.md"

#: Labels this repository uses for an authority it does not hold. Case-sensitive:
#: the lowercase words are ordinary prose ("blocked on a decision"), and a
#: classification is a label, so it has to look like one.
_BLOCKED = re.compile(
    r"(CLIENT-BLOCKED|VENDOR-BLOCKED|COMPLIANCE-BLOCKED|OPS-BLOCKED|"
    r"CLIENT-DEFERRED)")


def _read(path: pathlib.Path) -> str:
    assert path.is_file(), f"missing: {path}"
    return path.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def spec() -> str:
    return _read(SPEC)


@pytest.fixture(scope="module")
def adr() -> str:
    return _read(ADR)


def _paragraphs(text: str):
    """Claim scopes: a table row and a list item each stand alone; other prose
    is scoped to its paragraph.

    Same rule as the Week 7/8 status guards, for the same reason -- these
    documents put a requirement in one table cell and its blocker in the next,
    and a label three rows away is not a label on this row.

    List items count for the same reason, and mutation testing is what found it:
    the SAR section is a bullet list where every item ends in
    COMPLIANCE-BLOCKED, so a planted "a SAR must be filed within 30 days"
    bullet borrowed a label from its neighbours and passed. A bullet is a claim.
    """
    # Normalised first: `core.autocrlf` gives a Windows working tree CRLF while
    # CI checks out LF, so splitting on "\n\n" without this finds paragraph
    # breaks on one platform and not the other -- the guard would be strict in
    # CI and lax on the machine the author is looking at.
    # A list marker needs whitespace after it. Without that, `**Frequency, and
    # re-screening ... are not set here.**` reads as a bullet because the line
    # opens with an asterisk, and the sentence gets cut off from the clause that
    # carries its label -- which is how this function first reported a
    # correctly-labelled paragraph as unlabelled.
    is_row = re.compile(r"^\s*(?:[-*+]\s|\|)")

    for block in text.replace("\r\n", "\n").split("\n\n"):
        rows = [line for line in block.splitlines() if is_row.match(line)]
        if rows:
            for row in rows:
                yield row
            other = "\n".join(line for line in block.splitlines()
                              if not is_row.match(line))
            if other.strip():
                yield other
        else:
            yield block


def _flat(scope: str) -> str:
    return re.sub(r"\s+", " ", scope)


# --------------------------------------------------------------------------
# Both documents exist, and say what they are.
# --------------------------------------------------------------------------

def test_the_week9_documents_exist_and_are_paired(spec, adr):
    assert re.search(r"\*\*Status:\*\*\s*Accepted", spec), (
        "spec 0004 has no accepted status")
    assert re.search(r"\*\*Status:\*\*\s*Accepted", adr), (
        "ADR 0012 has no accepted status")

    assert "0012-sanctions-screening-integration.md" in spec, (
        "spec 0004 does not point at its ADR")
    assert "0004-kyc-aml-ubo-and-sanctions-screening.md" in adr, (
        "ADR 0012 does not point at its spec")


def test_neither_document_claims_the_screening_is_built(spec, adr):
    """The gap is visible today because `sanctions_screened` is a hardcoded
    False and D11 is open. A document that reads as an implementation report
    would hide it."""
    assert "sanctions_screened: bool = False" in _read(KYC_SCHEMAS), (
        "kyc-service no longer hardcodes sanctions_screened -- if screening was "
        "implemented, spec 0004 and ADR 0012 both need rewriting, and this "
        "guard is the wrong shape for the new world")

    debt = _read(DEBT)
    d11 = debt[debt.index("| **D11**"):]
    d11 = d11[:d11.index("\n|")] if "\n|" in d11 else d11
    assert re.search(r"\bOpen\b", d11), (
        "DEBT D11 no longer reads as open while the spec says it is unbuilt")

    # Each document must say, in its own words, what is NOT built. The phrasing
    # is deliberately loose -- the seam has since landed as mechanism only, so
    # "nothing is built" became untrue and had to be replaced by "nothing else
    # is built"; a guard pinned to one sentence would have blocked that
    # correction instead of checking the property.
    for label, text in (("spec 0004", spec), ("ADR 0012", adr)):
        assert re.search(r"nothing (?:in it |here |else )?is built|"
                         r"not (?:as )?(?:an )?implement|"
                         r"does not exist on `main`|spec(?:ification)? only|"
                         r"is not written|no migration is written|"
                         r"not written", text, re.I), (
            f"{label} does not say anywhere what is still unbuilt")

        # And the enforcement gap specifically, because that is the one a reader
        # would otherwise assume the seam closed.
        assert re.search(r"cip_passed`? is unchanged|no route (?:calls|wired)|"
                         r"wired into no route|no enforcement", text, re.I), (
            f"{label} does not say that nothing enforces a screen yet")


def test_the_spec_says_cip_is_not_full_kyc_aml(spec):
    """The client's premise -- "we verify identity already, so we're mostly
    there" -- is the thing the document exists to answer."""
    lowered = spec.lower()

    for concept in ("cdd", "ongoing monitoring", "sanctions", "sar"):
        assert concept in lowered, f"the spec does not distinguish {concept}"

    assert re.search(r"cip_passed`? MUST NOT be presented|"
                     r"is never described as KYC/AML compliance", spec), (
        "nothing forbids presenting cip_passed as KYC/AML compliance, which is "
        "the client's own mistake restated as a feature")

    # And nowhere may it assert the opposite. Mutation testing: rewriting §1's
    # prohibition into "cip_passed is the system's KYC/AML compliance evidence"
    # passed, because acceptance criterion 9 still carried the other half of the
    # alternation above.
    for scope in (_flat(s) for s in _paragraphs(spec)):
        if re.search(r"cip_passed`?\s+(?:is|as)\s+(?:the\s+)?(?:system's\s+)?"
                     r"KYC/AML compliance", scope, re.I):
            assert re.search(r"MUST NOT|never|not\b", scope, re.I), (
                f"the spec presents cip_passed as KYC/AML compliance "
                f"evidence:\n{scope.strip()[:240]}")


# --------------------------------------------------------------------------
# The model: ownership percentage, control person, entity -> owner.
# --------------------------------------------------------------------------

@pytest.mark.parametrize("required", [
    "beneficial_owners",
    "ownership_pct",
    "is_control_person",
    "capture_source",
])
def test_the_ownership_model_is_specified(spec, required):
    assert required in spec, f"the beneficial-ownership model does not define {required}"


def test_the_control_person_is_required_independently_of_the_percentage(spec):
    """Five 20% owners means no 25% owner and still one control person. A
    percentages-only model records nobody for that entity."""
    assert "25%" in spec, "the CDD threshold is not stated"
    assert re.search(r"whether or not.{0,60}25", spec, re.I | re.S), (
        "the spec does not say a control person is required even when no owner "
        "meets the 25% test")
    assert re.search(r"at most one control person|UNIQUE \(applicant_id\) WHERE is_control_person",
                     spec, re.I), (
        "nothing constrains an entity to one control person")


def test_the_ownership_chain_is_recorded_but_its_depth_is_not_invented(spec):
    chain = [s for s in (_flat(s) for s in _paragraphs(spec))
             if re.search(r"chain", s, re.I)]
    assert chain, "the spec does not address an entity owned by another entity"

    depth = [s for s in chain if re.search(r"deep|depth", s, re.I)]
    assert depth, "the spec does not address how far an ownership chain is walked"
    assert any(_BLOCKED.search(s) for s in depth), (
        "the spec picks an ownership-chain depth rule without a blocked label; "
        "there are two defensible rules and choosing is a policy decision")

    # And no scope may state a depth at all. Mutation testing: a planted "the
    # walk goes three levels deep, multiplying ownership through each link"
    # passed the check above simply by not containing the word "chain".
    prescribes_depth = re.compile(
        r"\b(?:one|two|three|four|five|\d+)\s+(?:levels?|links?|tiers?)\b"
        r"|\bdepth of\s+(?:one|two|three|four|five|\d+)\b", re.I)
    for scope in (_flat(s) for s in _paragraphs(spec)):
        found = prescribes_depth.search(scope)
        if found and not _BLOCKED.search(scope):
            pytest.fail(
                f"the spec prescribes an ownership-chain depth "
                f"({found.group(0)!r}) with no blocked label:\n"
                f"{scope.strip()[:240]}")


# --------------------------------------------------------------------------
# The provider seam.
# --------------------------------------------------------------------------

def test_the_provider_abstraction_is_specified_and_mirrors_the_bureau_seam(spec, adr):
    for label, text in (("spec 0004", spec), ("ADR 0012", adr)):
        assert "SanctionsScreeningProvider" in text, (
            f"{label} does not name the provider abstraction")
        assert "bureau" in text.lower(), (
            f"{label} does not tie the seam to the existing bureau boundary, "
            f"which is where this shape was already worked out")

    # The two defects that seam exists to prevent.
    assert re.search(r"query string|query parameter", spec, re.I), (
        "the spec does not forbid identity data in a query string -- the defect "
        "bureau.py was written to fix")
    assert re.search(r"request_key", spec), (
        "the spec does not require an idempotency key on a screen")


def test_idempotency_and_replay_are_specified(spec):
    replay = [s for s in (_flat(s) for s in _paragraphs(spec))
              if re.search(r"request_key|idempotenc", s, re.I)]
    assert replay, "no idempotency requirement at all"
    assert any(re.search(r"original", s, re.I) for s in replay), (
        "the spec does not require a replay to return the ORIGINAL screen; a "
        "second screen writes a second piece of evidence about one subject")


def test_the_provider_must_fail_closed(spec, adr):
    for label, text in (("spec 0004", spec), ("ADR 0012", adr)):
        assert re.search(r"fail clos", text, re.I), (
            f"{label} does not require fail-closed provider behaviour")

        # In body prose, not in a heading. Mutation testing: replacing the ADR's
        # fail-closed sentence with "onboarding proceeds; the screen is retried
        # later" still passed, because the section HEADING says "Fail closed,
        # with no degraded mode" and the check read the whole document.
        body = [s for s in (_flat(s) for s in _paragraphs(text))
                if not s.lstrip().startswith("#")]
        stated = [s for s in body
                  if re.search(r"timeout|transport error|malformed|error", s, re.I)
                  and re.search(r"refuse|MUST NOT|blocks rather than skips|"
                                r"cannot produce", s, re.I)]
        assert stated, (
            f"{label} names no failure that is actually refused -- a heading "
            f"saying 'fail closed' is not the rule")

        for scope in body:
            if re.search(r"(?:onboarding|the application) (?:proceeds|continues)"
                         r"|proceed(?:s|ing) without a (?:completed )?screen"
                         r"|screen(?:ing)? is (?:skipped|retried later)", scope, re.I):
                assert re.search(r"MUST NOT|no |never|rejected|would be", scope, re.I), (
                    f"{label} permits onboarding to continue without a "
                    f"completed screen:\n{scope.strip()[:240]}")

    assert not re.search(r"screening unavailable, proceed(?!ing\" path)", spec, re.I), (
        "the spec permits proceeding without a screen")


def test_provenance_is_required_and_the_raw_payload_is_not_stored(spec):
    assert "list_version" in spec, (
        "a screen with no list version is not reproducible evidence, and the "
        "spec does not require one")
    raw = [s for s in (_flat(s) for s in _paragraphs(spec))
           if re.search(r"raw_response|raw provider", s, re.I)]
    assert raw, "the spec does not say what happens to the provider's payload"
    assert any(re.search(r"NOT stored|not persisted", s, re.I) for s in raw), (
        "the spec does not forbid persisting the raw provider response")


# --------------------------------------------------------------------------
# The policy nobody here may write.
# --------------------------------------------------------------------------

def test_no_match_threshold_is_invented(spec, adr):
    """A number here looks like a control and is a guess with a compliance
    consequence in both directions."""
    fabricated = [
        r"threshold of \d",
        r"\d{1,3}\s?% (?:match|similarity|confidence)",
        r"match score (?:of|above|below|>=|<=|>|<) ?\d",
        r"score\s*(?:>=|<=|>|<|=)\s*\d",
        r"(?:jaro|levenshtein|soundex|metaphone)\w*\s*(?:of|=|>=)\s*[\d.]",
    ]
    for label, text in (("spec 0004", spec), ("ADR 0012", adr)):
        for pattern in fabricated:
            for match in re.finditer(pattern, text, re.I):
                scope = _flat(text[max(0, text.rfind("\n\n", 0, match.start())):
                                   text.find("\n\n", match.end())])
                pytest.fail(
                    f"{label} states a match threshold ({match.group(0)!r}), "
                    f"which is COMPLIANCE-BLOCKED and VENDOR-BLOCKED:\n"
                    f"{scope.strip()[:240]}")

    # The label has to sit in the section that discusses thresholds. Mutation
    # testing: deleting it from there still passed, because the blocked table
    # forty paragraphs later mentions "threshold" and carries a label.
    section = spec[spec.index("Matching thresholds"):]
    section = section[:section.index("\n## ")]
    labelled = [s for s in (_flat(s) for s in _paragraphs(section))
                if re.search(r"threshold", s, re.I) and _BLOCKED.search(s)]
    assert labelled, (
        "the section on match thresholds carries no blocked label, so a reader "
        "cannot tell whether the absence of a number is a decision or an "
        "omission")


def test_the_disposition_of_a_potential_match_is_not_invented(spec):
    assert "potential_match" in spec, (
        "the spec has no third outcome; clear/hit alone forces a verdict the "
        "provider did not give")

    disposition = [s for s in (_flat(s) for s in _paragraphs(spec))
                   if re.search(r"potential[ _]match", s, re.I)
                   and re.search(r"who may clear|disposition", s, re.I)]
    assert disposition, "the spec never addresses who disposes of a match"
    assert any(_BLOCKED.search(s) for s in disposition), (
        "the spec assigns a disposition policy without a blocked label")

    assert re.search(r"MUST NOT auto-resolve", spec), (
        "nothing forbids a potential match resolving itself to clear, which is "
        "the one disposition rule that needs no authority")


def test_no_sar_rule_or_escalation_owner_is_invented(spec, adr):
    for label, text in (("spec 0004", spec), ("ADR 0012", adr)):
        for scope in (_flat(s) for s in _paragraphs(text)):
            if not re.search(r"\bSAR\b", scope):
                continue
            if re.search(r"file a SAR (?:when|if|within)|must be (?:filed|reported) "
                         r"within|\bwithin \d+ (?:days|hours)\b|"
                         r"(?:the )?(?:BSA officer|compliance officer) (?:files|decides|signs)",
                         scope, re.I):
                assert _BLOCKED.search(scope), (
                    f"{label} states a SAR rule or names a filing owner without a "
                    f"blocked label:\n{scope.strip()[:240]}")

    sar = [s for s in (_flat(s) for s in _paragraphs(spec))
           if re.search(r"\bSAR\b", s)]
    assert any(_BLOCKED.search(s) for s in sar), (
        "spec 0004 discusses SAR without classifying it as blocked anywhere")


def test_every_blocked_authority_names_its_label(spec):
    """The blocked table is the operative output of this spec: it is what turns
    "not built" into "waiting on a named decision"."""
    for label in ("COMPLIANCE-BLOCKED", "VENDOR-BLOCKED", "CLIENT-BLOCKED",
                  "OPS-BLOCKED"):
        assert label in spec, f"spec 0004 uses no {label} classification"

    # Every row of the blocked table must classify itself. Mutation testing: a
    # row rewritten to "collected by default" passed, because the labels it no
    # longer carried were still present elsewhere in the document -- which is
    # exactly the row a reader would take as an approved decision.
    table = spec[spec.index("## Blocked, and by whom"):]
    table = table[:table.index("\n## ")]
    rows = [r for r in table.splitlines()
            if r.lstrip().startswith("|") and not set(r) <= set("|- ")]
    data_rows = [r for r in rows if "---" not in r][1:]
    assert len(data_rows) >= 5, (
        f"the blocked table has only {len(data_rows)} rows; the spec identifies "
        f"more unavailable authorities than that in its own text")
    for row in data_rows:
        assert _BLOCKED.search(row), (
            f"a row in the blocked table names no blocking party, so it reads "
            f"as a decision this repository made:\n{row.strip()[:200]}")

    assert "## Non-goals" in spec, (
        "a spec without non-goals invites the scope it never agreed to -- and "
        "this brief explicitly asked for a launch-ready appearance")
    assert re.search(r"look thorough", spec, re.I), (
        "the spec does not quote and refuse the client's actual ask")


def test_no_vendor_is_named_as_selected(spec, adr):
    """Naming a provider as chosen is a procurement decision. The word may
    appear in the sentence that says none is selected."""
    for label, text in (("spec 0004", spec), ("ADR 0012", adr)):
        for scope in (_flat(s) for s in _paragraphs(text)):
            if re.search(r"(?:we|Meridian) (?:will |now )?(?:use|integrate with|"
                         r"have selected|selected)\s+\w+", scope, re.I):
                assert re.search(r"no vendor|not selected|VENDOR-BLOCKED", scope, re.I), (
                    f"{label} reads as having selected a screening vendor:\n"
                    f"{scope.strip()[:240]}")

    assert re.search(r"[Nn]o (?:screening )?(?:provider|vendor) is selected", spec + adr), (
        "neither document says plainly that no vendor is selected")


def test_the_stub_may_not_carry_sanctions_list_like_data(spec, adr):
    combined = spec + adr
    assert re.search(r"MUST NOT ship (?:with )?names", combined), (
        "nothing forbids a stub shipping names that resemble real SDN entries, "
        "which would create a file people mistake for the list")

    # And neither document may permit it in its own words. Mutation testing: the
    # prohibition living in one document let the other say the stub ships sample
    # SDN entries "for realism", because the phrase check passed on the copy.
    for label, text in (("spec 0004", spec), ("ADR 0012", adr)):
        for scope in (_flat(s) for s in _paragraphs(text)):
            hit = re.search(
                r"[^.]*\bships?\b[^.]{0,60}\b(?:SDN|sanctions[- ]list)[^.]*",
                scope, re.I)
            if hit:
                # Sentence-scoped, not paragraph-scoped: the paragraph also
                # contains the sentence explaining why list-like data must not
                # be committed, and a paragraph-wide check let a planted "ships
                # a sample of real SDN entries for realism" borrow it as an
                # alibi.
                assert re.search(r"MUST NOT|never|would create a file",
                                 hit.group(0), re.I), (
                    f"{label} permits shipping sanctions-list-like data:\n"
                    f"{hit.group(0).strip()[:240]}")


# --------------------------------------------------------------------------
# Monitoring triggers: the part a spec CAN give without policy.
# --------------------------------------------------------------------------

def test_monitoring_trigger_points_are_specified_without_a_cadence(spec):
    triggers = spec[spec.index("Ongoing monitoring and the SAR boundary"):]
    for trigger in ("new application", "beneficial_owners", "list refresh"):
        assert re.search(trigger, triggers, re.I), (
            f"the monitoring triggers do not include {trigger!r}, which the "
            f"repository's own data model already makes identifiable")

    cadence = [s for s in (_flat(s) for s in _paragraphs(triggers))
               if re.search(r"cadence|frequency", s, re.I)]
    assert cadence, "the spec does not address re-screening frequency at all"
    assert all(_BLOCKED.search(s) for s in cadence), (
        "the spec sets a re-screening cadence; that is a cost and coverage "
        "decision, not an engineering one")

    assert not re.search(r"(?:re-?screen|screen).{0,40}\b(?:daily|weekly|monthly|"
                         r"every \d+ (?:days|hours))\b", spec, re.I), (
        "the spec fixes a screening interval")


# --------------------------------------------------------------------------
# Acceptance criteria have to be checkable, and one of them has a fixture.
# --------------------------------------------------------------------------

def test_the_acceptance_criteria_bind_cip_passed_to_a_completed_screen(spec):
    # Flattened: the criterion wraps across lines in the document, and a
    # line-sensitive search would depend on where it happens to wrap.
    assert re.search(r"No applicant reaches `cip_passed = true` without a "
                     r"completed sanctions screen", _flat(spec)), (
        "the brief's own example acceptance criterion is missing")

    assert re.search(r"Northgate", spec) or re.search(r"Northgate", _read(ADR)), (
        "neither document names the seeded entity that would fail the new rule, "
        "so the criterion has no worked fixture")


def test_the_spec_does_not_claim_regulatory_compliance(spec):
    """This is a local training build. Naming a rule identifies what a control
    is modelled on, and several controls here are explicitly non-compliant."""
    assert re.search(r"modelled on|not a statement that Meridian complies|"
                     r"reviewed by counsel", spec, re.I), (
        "the spec cites regulations without stating they are what a control is "
        "modelled on rather than a compliance claim")

    for match in re.finditer(r"complian(?:t|ce)", spec, re.I):
        scope = _flat(spec[max(0, spec.rfind("\n\n", 0, match.start())):
                           spec.find("\n\n", match.end())])
        if re.search(r"Meridian (?:is|are) complian|"
                     r"(?:this|the) (?:spec|design|system) (?:is|ensures) complian",
                     scope, re.I):
            pytest.fail(f"spec 0004 asserts compliance: {scope.strip()[:240]}")
