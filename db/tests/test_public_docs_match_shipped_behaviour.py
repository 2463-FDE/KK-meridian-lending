"""The public documents may not contradict what the repository ships.

Both contradictions this file exists to prevent were live on `main` on
2026-08-27, on the day of a client-facing review, and both had the same shape:
a sentence that was true when written, sitting next to a fact that had moved,
with nothing forcing the two to agree.

  * `README.md` said the AI underwriting assistant "has not been started" while
    `services/loan-assistant/` was a Compose service behind a staff-only gateway
    route, tested, merged and running.
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
    _PAST = re.compile(r"originally|delivered|was |used to|Halcyon|handed over",
                       re.I)
    claims = []
    for word, n in words.items():
        for m in re.finditer(r"\*\*%s\*\* backend services" % word, text):
            before = text[max(0, m.start() - 160):m.start()]
            if not _PAST.search(before):
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
                f"staff-only gateway route:\n  ...{window.strip()[:260]}...")


def test_the_readme_names_the_assistant_service():
    """Naming it is what makes the count checkable by a human reader too."""
    if "loan-assistant" not in backend_services():
        pytest.skip("loan-assistant is not a service")

    text = README.read_text(encoding="utf-8")
    assert "loan-assistant" in text, (
        "README does not mention loan-assistant anywhere, so a reader counting "
        "the services it lists cannot reach the number it claims")


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
