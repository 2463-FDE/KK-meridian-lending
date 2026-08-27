"""The public documents may not contradict what the repository ships.

Both contradictions this file exists to prevent were live on `main` on
2026-08-27, on the day of a client-facing review, and both had the same shape:
a sentence that was true when written, sitting next to a fact that had moved,
with nothing forcing the two to agree.

  * `README.md` said the AI underwriting assistant "has not been started" while
    `services/loan-assistant/` was a Compose service behind live gateway routes,
    tested, merged and running.
  * `docs/ROADMAP.md` said "Maker-checker remains Not started" **eleven lines
    below its own count of `0 Not started`**, and while `docs/DEBT.md` D8 read
    `Fixed`. The flagship money control, described in the flagship document as
    not built.

The second is the expensive one. A reviewer reading it in front of a client is
told the demo they are about to watch does not exist.

**Derived, not hand-maintained.** The service count comes from counting
`services/*/`, because a hand-maintained number in prose is exactly what went
stale -- README said seven, `ARCHITECTURE.md` said eight, and both were edited by
the same people. A number a test recomputes cannot drift; a number two documents
each remember separately always will.

**What this deliberately does not do.** It does not ban the words. Historical
notes, ADRs describing what was true at the time, and the status legend that
defines "Not started" as a vocabulary term all need to say these things. What is
checked is a *live claim* about current state.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
README = REPO / "README.md"
ROADMAP = REPO / "docs" / "ROADMAP.md"
DEBT = REPO / "docs" / "DEBT.md"
COMPOSE = REPO / "docs" / ".." / "docker-compose.yml"

#: Marks a sentence as a record of what a document used to say. The repository's
#: house style, and the same narrow shape PR #107 settled on: no `re.S`, cannot
#: cross an emphasis marker, and the closing `*` must close something.
_HISTORY_NOTE = re.compile(r"\*This (?:cell|clause|paragraph|row|section|file) read[^*]*\*(?=\s|$)")


def _live(text: str) -> str:
    """The document with its dated history notes removed."""
    return _HISTORY_NOTE.sub("", text)


def backend_services() -> set:
    """The backend services, counted rather than remembered.

    `services/*/` is the definition. `reconciliation` in `docker-compose.yml`
    builds from `./services/servicing-service` and runs a scheduled command, so
    it is that image doing a job rather than a service of its own -- which is
    precisely the kind of distinction a prose number gets wrong.
    """
    return {p.name for p in (REPO / "services").iterdir()
            if p.is_dir() and (p / "app").is_dir()}


def test_the_service_inventory_is_what_the_readme_says():
    """README's count must equal the number of services that exist."""
    actual = backend_services()
    text = README.read_text(encoding="utf-8")

    words = {"three": 3, "four": 4, "five": 5, "six": 6, "seven": 7,
             "eight": 8, "nine": 9, "ten": 10}

    # Only PRESENT-TENSE counts. README opens by saying Halcyon originally
    # delivered three backend services, which is true and must keep being
    # sayable -- a test that forced every number in the file to equal today's
    # count would delete the history to satisfy itself.
    # Scoped to the SENTENCE, not a character window. A fixed 160-character
    # lookback let "The platform was decomposed and now runs **seven** backend
    # services" evade the check, because `was` appeared earlier in a sentence
    # whose claim is present-tense. Sentence boundaries are what "this clause is
    # about the past" actually means.
    _PAST = re.compile(r"\b(originally|delivered|used to|Halcyon|handed over)\b",
                       re.I)
    _PRESENT = re.compile(r"\b(now runs|currently|today|the platform runs)\b", re.I)
    claims = []
    for word, n in words.items():
        for m in re.finditer(r"\*\*%s\*\* backend services" % word, text):
            start = max(text.rfind(". ", 0, m.start()),
                        text.rfind("\n\n", 0, m.start())) + 1
            sentence = text[start:m.end()]
            historical = _PAST.search(sentence) and not _PRESENT.search(sentence)
            if not historical:
                claims.append((word, n))

    assert claims, (
        "README no longer states a present-tense backend-service count in the "
        "form '**N** backend services'. If the wording changed deliberately, "
        "update this test; if the sentence was deleted, the count it carried "
        "was the thing keeping README and ARCHITECTURE.md honest with each other.")

    for word, claimed in claims:
        assert claimed == len(actual), (
            f"README says **{word}** ({claimed}) backend services; "
            f"{len(actual)} exist: {sorted(actual)}. "
            f"Counted from services/*/ rather than trusted, because this exact "
            f"number was stale on main for weeks while ARCHITECTURE.md had it right.")


def test_the_readme_does_not_call_a_shipped_service_unstarted():
    """The claim that cost the most in front of a client.

    Conditional on the service existing: if `loan-assistant` were ever removed,
    README *should* be able to say the work is not started, and this test must
    not stand in the way of that.
    """
    if "loan-assistant" not in backend_services():
        pytest.skip("loan-assistant is not a service; README may say so")

    live = _live(README.read_text(encoding="utf-8"))

    for phrase in ("has not been started", "have not been started",
                   "not yet been started", "work has not begun"):
        for match in re.finditer(re.escape(phrase), live, re.I):
            window = live[max(0, match.start() - 400):match.end() + 200]
            assert not re.search(r"assistant|agent", window, re.I), (
                f"README says {phrase!r} near a mention of the assistant, but "
                f"services/loan-assistant/ is a live Compose service behind a "
                f"gateway route:\n  ...{window.strip()[:260]}...")


def _services_table_rows(text: str) -> set:
    """The `services/<name>/` paths listed in README's Services table."""
    return set(re.findall(r"\|\s*`services/([a-z0-9-]+)/`\s*\|", text))


def _architecture_diagram(text: str) -> str:
    """The fenced block under `## Architecture`."""
    m = re.search(r"^## Architecture\s*\n+```(.*?)```", text, re.S | re.M)
    return m.group(1) if m else ""


def test_the_services_table_lists_every_service():
    """The count in prose is not where a reader looks. The table is.

    This is the finding that came back on the first version of this PR: the
    prose was corrected to eight and the Services table still listed seven,
    with no `loan-assistant` row. The drift moved rather than closing, and the
    guard did not catch it because it only asked whether the name appeared
    *anywhere* -- and it now appeared, in the sentence I had just written.

    Asserting on the table is what makes the number checkable by a human, who
    counts rows rather than trusting an adjective.
    """
    text = README.read_text(encoding="utf-8")
    listed, actual = _services_table_rows(text), backend_services()

    assert listed == actual, (
        f"README's Services table lists {sorted(listed)}; the repository has "
        f"{sorted(actual)}. Missing from the table: {sorted(actual - listed)}. "
        f"Listed but absent: {sorted(listed - actual)}.")


def test_the_architecture_diagram_shows_every_service():
    """The other place a reader looks, and the one a client screenshots."""
    diagram = _architecture_diagram(README.read_text(encoding="utf-8"))
    assert diagram, "README no longer has a fenced Architecture diagram"

    missing = sorted(s for s in backend_services() if s not in diagram)
    assert missing == [], (
        f"the architecture diagram omits {missing}. A diagram that denies a "
        f"service ships is the version of this claim a client actually reads.")


def test_the_gateway_routes_in_the_diagram_match_the_gateway():
    """The diagram's route list is a claim about the gateway, so check it.

    Without this, a service can be drawn in the diagram while the prefix that
    reaches it is missing from the line above -- which is what happened to
    `/assistant`.
    """
    diagram = _architecture_diagram(README.read_text(encoding="utf-8"))
    gateway = (REPO / "services" / "gateway" / "app" / "main.py").read_text(encoding="utf-8")

    prefixes = set(re.findall(r'@app\.api_route\("/([a-z-]+)/\{path:path\}"', gateway))
    missing = sorted(p for p in prefixes if f"/{p}" not in diagram)

    assert missing == [], (
        f"the gateway proxies {missing} but the diagram's route list does not "
        f"show them")


def test_the_roadmap_does_not_call_maker_checker_unstarted():
    """The contradiction sat eleven lines below the count that refuted it."""
    live = _live(ROADMAP.read_text(encoding="utf-8"))

    offenders = []
    for match in re.finditer(r"[Mm]aker.?checker", live):
        window = live[match.start():match.start() + 220]
        if re.search(r"\bNot started\b", window):
            offenders.append(window.strip()[:200])

    assert offenders == [], (
        "docs/ROADMAP.md describes maker-checker as Not started. It is "
        "implemented: proposals return 202 and move nothing, self-approval is "
        "refused including for admin, and an approval writes exactly one ledger "
        "entry. See docs/DEBT.md D8.\n  " + "\n  ".join(offenders))


def test_the_roadmap_count_and_its_prose_agree():
    """Whatever the matrix counts, the prose may not contradict it.

    The general form of the specific bug: a status total and a sentence about
    the same status, neither aware of the other.

    Known limit, tracked rather than hidden: this only fires when the total is
    `0`. A roadmap counting `3 Not started` with five items named in prose would
    pass. Generalising needs a way to tie prose items to matrix rows, which is a
    bigger change than this PR should carry -- but `0` is the case that actually
    broke, and the case a finished project ends in.
    """
    live = _live(ROADMAP.read_text(encoding="utf-8"))
    match = re.search(r"(\d+)\s+Not started", live)
    if not match:
        pytest.skip("the roadmap no longer states a Not-started total")

    total = int(match.group(1))
    if total == 0:
        prose = [m.group(0) for m in
                 re.finditer(r"^[A-Z][^|\n]{0,120}\bremains Not started\b[^|\n]*",
                             live, re.M)]
        assert prose == [], (
            f"the roadmap counts {total} Not started and then says otherwise in "
            f"prose:\n  " + "\n  ".join(prose))


def test_debt_agrees_that_the_maker_checker_row_is_closed():
    """Cross-document: the claim above rests on D8, so D8 has to still say it."""
    text = DEBT.read_text(encoding="utf-8")
    row = re.search(r"^\|\s*\*\*D8\*\*\s*\|(.*)$", text, re.M)
    assert row, "docs/DEBT.md no longer has a D8 row"

    status = [c.strip() for c in row.group(1).split("|")][1]
    assert re.search(r"\bFixed\b", status), (
        "D8 no longer reads as fixed, which is the evidence the roadmap "
        "correction and this test both rest on: "
        f"{status[:160]}")


def _handler_body(source: str, decorator: str) -> str:
    """Everything from a route decorator to the next one.

    String slicing rather than a regex: these handlers carry long comment blocks
    containing braces and quotes, and every regex attempt either stopped at the
    first `#` or failed to anchor. The boundary is simple, so the code is too.
    """
    start = source.find(decorator)
    if start < 0:
        return ""
    nxt = source.find("\n@app.", start + len(decorator))
    return source[start:nxt if nxt > 0 else len(source)]


def _gateway_source() -> str:
    return (REPO / "services" / "gateway" / "app" / "main.py").read_text(encoding="utf-8")


def test_the_readme_does_not_call_an_open_route_staff_only():
    """The claim this PR shipped and had to take back.

    Correcting "the assistant has not been started" produced a *new* false
    sentence in its place: `/assistant` described as a staff-only gateway route.
    It is two routes with two gates, deliberately.

    `POST /assistant/applications/{id}/summary` returns per-applicant financials
    and requires a staff session. `POST /assistant/policy-chat` returns generic
    lending policy, carries no applicant data, and is anonymous-allowed on
    purpose -- `test_assistant_policy_chat_proxies_anonymously_with_no_session`
    and `test_assistant_policy_chat_allows_borrower_role` both assert it.

    A truth-keeping change that replaces a stale claim with a false one is worse
    than what it removed, because it arrives carrying fresh credibility. So the
    wording is tied to the gateway's own code rather than to anyone's memory.
    """
    gateway = _gateway_source()

    summary_body = _handler_body(gateway, '@app.api_route("/assistant/{path:path}"')
    assert "is_staff" in summary_body, (
        "the /assistant catch-all no longer applies a staff check; the README "
        "wording asserted below rests on that check existing")

    chat_body = _handler_body(gateway, '@app.post("/assistant/policy-chat")')
    assert chat_body, "the /assistant/policy-chat route is gone"

    if "_require_user" in chat_body:
        pytest.skip("policy-chat now requires a session; README may say staff-only")

    text = README.read_text(encoding="utf-8")

    offenders = []
    for m in re.finditer(r"staff[- ]only", text, re.I):
        window = text[max(0, m.start() - 240):m.end() + 240]
        # A staff-only claim is fine when it is about the summary route. It is
        # false when it is about the /assistant prefix as a whole.
        about_summary = "summary" in window.lower()
        about_prefix = re.search(r"/assistant(?![/\w])", window)
        if about_prefix and not about_summary:
            offenders.append(window.strip()[:200])

    assert offenders == [], (
        "README calls /assistant staff-only, but /assistant/policy-chat is "
        "anonymous-allowed in the gateway and two gateway tests assert it:\n  "
        + "\n  ".join(offenders))

    assert re.search(r"policy-chat.{0,160}(anonymous|open to|without an account)",
                     text, re.I | re.S), (
        "README does not say /assistant/policy-chat is anonymous-allowed, so a "
        "reader carries the staff gate across both routes")
