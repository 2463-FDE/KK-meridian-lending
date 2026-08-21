"""Every path a slide cites must resolve, or be labelled as not yet landed.

A deck is read once, out loud, by someone who cannot check a path mid-sentence.
So the rule is stricter than for prose: a cited file either exists on this branch,
or the slide says explicitly that it is a draft on an open PR.

The failure this prevents is specific and was live in the first version: slide 1
cited `specs/0002` as though the specification were part of the system. It is a
draft on PR #28. Presenting proposed work as landed is the same defect as a policy
publishing a rule nothing implements -- an audience cannot tell the difference,
and here the audience is the client.
"""
import pathlib
import re
import subprocess

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
PRESENTATIONS = REPO / "docs" / "presentations"

#: EVERY deck, not one named file. The client asked for the deck to be
#: regenerated from the repository before each demo, so there is now more than
#: one -- and this module was pinned to `2026-08-12-three-slides.md` by name.
#: A second deck could have cited a path that does not exist, or a PR that was
#: never merged, and the suite would have stayed green while the guard sat
#: watching last month's file. The coverage stopping where the author last
#: looked is the same defect this file exists to catch in a deck.
DECKS = sorted(PRESENTATIONS.glob("*-three-slides.md"))

#: Findings specific to the first deck's own slides. They are assertions about
#: what that deck must keep saying, so they stay pinned to it rather than being
#: imposed on every future deck.
LEGACY_DECK = PRESENTATIONS / "2026-08-12-three-slides.md"

pytestmark = pytest.mark.skipif(not DECKS, reason="no deck on this branch")

#: Words that mark a citation as proposed rather than landed.
OPEN_MARKERS = ("not yet on `main`", "open — pr", "open -- pr", "draft in pr",
                "**open**", "not started", "not evidenced", "open gap")


def _cited_paths(deck):
    text = deck.read_text(encoding="utf-8")
    return sorted(set(re.findall(r"`((?:db|docs|services|specs|frontend|scripts|policies)/[\w./\-]+)`", text)))


def _line_context(deck, needle):
    text = deck.read_text(encoding="utf-8").splitlines()
    return [l.lower() for l in text if needle in l]


def _deck_paths():
    """(deck, path) for every citation in every deck."""
    return [(deck, path) for deck in DECKS for path in _cited_paths(deck)]


@pytest.mark.parametrize("deck,path", _deck_paths(),
                         ids=lambda v: v.name if hasattr(v, "name") else v)
def test_every_cited_path_resolves_or_is_labelled_open(deck, path):
    if (REPO / path).exists():
        return
    context = " ".join(_line_context(deck, path))
    assert any(m in context for m in OPEN_MARKERS), (
        f"{deck.name} cites {path}, which does not exist on this branch and is "
        f"not labelled as a draft on an open PR. An audience cannot tell "
        f"proposed work from landed work, and here the audience is the client."
    )


@pytest.mark.parametrize("deck", DECKS, ids=lambda d: d.name)
def test_the_deck_cites_the_debt_register_by_its_real_path(deck):
    """`DEBT.md` does not exist at the repository root; `docs/DEBT.md` does. A
    citation that does not resolve is a broken claim, and this one is quoted as
    the tracking record for an open control gap."""
    text = deck.read_text(encoding="utf-8")
    assert "docs/DEBT.md" in text
    bare = re.findall(r"(?<!/)(?<!docs/)`DEBT\.md`", text)
    assert not bare, f"{deck.name} cites a bare DEBT.md {len(bare)} time(s)"


@pytest.mark.parametrize("deck", DECKS, ids=lambda d: d.name)
def test_it_found_paths_to_check(deck):
    assert len(_cited_paths(deck)) >= 8, (
        f"{deck.name} cites almost nothing -- check the regex")


@pytest.mark.skipif(not LEGACY_DECK.is_file(), reason="the 2026-08-12 deck is not on this branch")
def test_the_oracle_is_not_described_as_an_external_authority():
    """The golden file is a checked-in artifact this team produced. Calling it a
    third party overstates it -- nobody outside the repository certified it.

    Pinned to the deck that makes the claim: a later deck that does not mention
    the golden vectors at all must not be required to describe them."""
    text = LEGACY_DECK.read_text(encoding="utf-8").lower()
    assert "independent third party" not in text, (
        "the deck calls the golden vector file an independent third party"
    )
    assert "oracle artifact" in text, (
        "the deck does not say what the golden file actually is"
    )


@pytest.mark.skipif(not LEGACY_DECK.is_file(), reason="the 2026-08-12 deck is not on this branch")
def test_the_servicing_float_boundary_is_on_the_slide_not_only_in_the_notes():
    """A limitation that lives only in speaker notes is not disclosed to anyone
    reading the deck afterwards. Pinned to the deck that raised it."""
    text = LEGACY_DECK.read_text(encoding="utf-8")
    slide3 = text[text.index("## Slide 3"):]
    on_screen = slide3[:slide3.index("### Notes")]
    assert "float" in on_screen.lower(), (
        "the servicing float boundary appears only in the notes, so it is absent "
        "from what the room and any later reader actually see"
    )


# --- PR-backed claims ---------------------------------------------------------
#
# The resolver above only validated backticked repository paths, so `PR #22`,
# `PR #23` and `PR #24` passed through unchecked. A wrong number, a deleted PR
# or one that quietly reopened would still have gone green -- and those PRs are
# what back the internal-token and Decimal-boundary claims on the slides.
#
# A pull request is not durable evidence: it can be renumbered or reopened, and
# a reader offline cannot check it at all. So each one is resolved through
# docs/presentations/evidence-manifest.md to something that survives the PR.

MANIFEST = PRESENTATIONS / "evidence-manifest.md"


def _manifest_rows():
    rows = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        m = re.match(r"\|\s*#(\d+)\s*\|\s*(\w+)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*$", line)
        if m:
            number, status, artifact, commit = m.groups()
            path = re.search(r"`([^`]+)`", artifact)
            rows[int(number)] = {
                "status": status.lower(),
                "artifact": path.group(1) if path else None,
                "commit": (re.search(r"`([0-9a-f]{7,40})`", commit) or [None, None])[1],
            }
    return rows


def _cited_prs():
    """Every PR number cited by ANY deck, deduplicated.

    Across decks rather than per deck: one manifest resolves them all, so a PR
    introduced by a newer deck has to be listed there too."""
    numbers = set()
    for deck in DECKS:
        numbers.update(int(n) for n in re.findall(r"PR #(\d+)", deck.read_text(encoding="utf-8")))
    return sorted(numbers)


def _deck_prs():
    """(deck, pr) for every PR cited by each deck, kept PER DECK.

    Reviewed on PR #55 (PR-GUARD-001). Widening the module to many decks turned
    this into a set of numbers and joined every deck's lines together as the
    context, so the open-PR label check leaked across files: a new deck could
    cite an open PR with no open marker and pass, because an OLDER deck supplied
    the marker. The label is a property of the slide a reader is looking at, so
    it has to be asserted against that slide's own text -- the same
    (deck, path) shape the path guard already uses.
    """
    pairs = []
    for deck in DECKS:
        text = deck.read_text(encoding="utf-8")
        for pr in sorted(set(int(n) for n in re.findall(r"PR #(\d+)", text))):
            pairs.append((deck, pr))
    return pairs


def test_the_manifest_exists():
    assert MANIFEST.is_file(), (
        "the deck cites pull requests but there is no evidence manifest to "
        "resolve them against"
    )


@pytest.mark.parametrize("pr", _cited_prs())
def test_every_cited_pr_is_in_the_manifest(pr):
    assert pr in _manifest_rows(), (
        f"the deck cites PR #{pr}, which is not in evidence-manifest.md. An "
        f"audience cannot verify a bare PR number, and it stops resolving the "
        f"moment the PR is archived or renumbered."
    )


@pytest.mark.parametrize("pr", _cited_prs())
def test_a_merged_pr_names_an_artifact_that_exists(pr):
    row = _manifest_rows().get(pr) or {}
    # An unrecognised status must not slip past as "not merged, nothing to do".
    # test_every_manifest_status_is_recognised owns that failure; this asserts
    # the vocabulary here too, so tightening one check cannot quietly loosen
    # this one.
    assert row.get("status") in MANIFEST_STATUSES, (
        f"PR #{pr} has an unrecognised status {row.get('status')!r}"
    )
    if row["status"] != "merged":
        return
    artifact = row.get("artifact")
    assert artifact, f"PR #{pr} is listed merged with no durable artifact"
    assert (REPO / artifact).exists(), (
        f"PR #{pr} cites {artifact}, which does not exist on this branch -- so "
        f"the claim it backs cannot be checked by anyone reading the deck."
    )


@pytest.mark.parametrize("deck,pr", _deck_prs(),
                         ids=lambda v: v.name if hasattr(v, "name") else v)
def test_an_open_pr_is_labelled_open_in_the_deck(deck, pr):
    """An open PR has no landed artifact, so the deck citing it must say so.

    Per deck, not per PR number: a marker in one deck says nothing about what a
    reader of another deck can see.
    """
    row = _manifest_rows().get(pr) or {}
    assert row.get("status") in MANIFEST_STATUSES, (
        f"PR #{pr} has an unrecognised status {row.get('status')!r}"
    )
    if row["status"] != "open":
        return
    context = " ".join(_line_context(deck, f"PR #{pr}"))
    assert any(m in context for m in OPEN_MARKERS), (
        f"{deck.name} cites PR #{pr}, which has not landed, without an open "
        f"marker on the line. That is the overclaim this test exists to catch, "
        f"and another deck labelling it correctly does not help this reader."
    )


@pytest.mark.parametrize("pr", _cited_prs())
def test_a_recorded_merge_commit_resolves(pr):
    """Checked when the object is present.

    CI checks out with limited history, so this assertion is skipped -- with a
    reason -- on a shallow clone rather than passing silently. The artifact
    assertion above never skips, so no row is ever accepted on no evidence.
    """
    row = _manifest_rows().get(pr) or {}
    commit = row.get("commit")
    if not commit:
        return
    if subprocess.run(["git", "rev-parse", "--is-shallow-repository"],
                      cwd=REPO, capture_output=True, text=True).stdout.strip() == "true":
        pytest.skip("shallow clone -- merge commits are not in this checkout")
    found = subprocess.run(["git", "cat-file", "-e", f"{commit}^{{commit}}"],
                           cwd=REPO, capture_output=True)
    assert found.returncode == 0, (
        f"PR #{pr} records merge commit {commit}, which is not in this repository"
    )


def test_the_pr_checks_are_not_vacuous():
    """A parametrized test over an empty list passes and proves nothing."""
    assert len(_cited_prs()) >= 3, f"found almost no PR citations: {_cited_prs()}"
    merged = [p for p, r in _manifest_rows().items() if r["status"] == "merged"]
    assert merged, "the manifest lists no merged PR, so the artifact check never ran"

# --- manifest status validation ----------------------------------------------
#
# The two checks above each return early when the status is not the one they
# handle. That is fine for the two statuses that exist and silently wrong for
# any other: a row typed `landed`, `merge` or `closed` matched neither branch,
# so it was never checked for an artifact AND never required an open label. The
# manifest could carry a status nothing understood and the suite stayed green --
# an unrecognised value read as "checked" when it meant "skipped".
#
# So the vocabulary is closed. Exactly two statuses are legal, and anything else
# is an explicit failure rather than a quiet pass.

MANIFEST_STATUSES = ("merged", "open")


def _status_problem(pr, row):
    """Return a human-readable problem with this row, or None if it is sound.

    Pure and row-shaped on purpose: a synthetic row can be passed straight in,
    so the rejection of an unsupported status is provable without writing a
    broken manifest to disk and hoping the parser sees it the same way.
    """
    status = (row or {}).get("status")
    if status not in MANIFEST_STATUSES:
        return (
            f"PR #{pr} has status {status!r}, which is not one of "
            f"{list(MANIFEST_STATUSES)}. An unrecognised status is skipped by "
            f"every other check, so the row would be accepted on no evidence."
        )
    if status == "merged" and not (row or {}).get("artifact"):
        return (
            f"PR #{pr} is listed merged with no durable artifact. A merged row "
            f"is the one thing a reader can verify offline; without a file it "
            f"is a bare PR number again."
        )
    if status == "open" and (row or {}).get("artifact"):
        return (
            f"PR #{pr} is open but names artifact {row['artifact']!r}. Open "
            f"means nothing has landed, so a row that points at a file invites "
            f"the reader to believe proposed work has repository evidence -- "
            f"the same overclaim in a new column."
        )
    return None


@pytest.mark.parametrize("pr", sorted(_manifest_rows()))
def test_every_manifest_status_is_recognised(pr):
    problem = _status_problem(pr, _manifest_rows()[pr])
    assert problem is None, problem


@pytest.mark.parametrize("status", ["landed", "merge", "closed", "verified",
                                    "specified", "draft", "", "MERGED?"])
def test_an_unsupported_status_is_rejected(status):
    """The regression proof for the finding.

    Each of these previously produced a green run: neither the artifact check
    nor the open-label check claimed the row, so nothing looked at it. `landed`
    and `merge` are the realistic typos -- both are words this deck legitimately
    uses elsewhere, which is exactly why one would be easy to write here.
    """
    problem = _status_problem(99, {"status": status, "artifact": "docs/DEBT.md"})
    assert problem is not None, (
        f"status {status!r} was accepted; an unrecognised status is skipped by "
        f"every other check, so the row passes on no evidence"
    )
    assert "not one of" in problem


def test_a_merged_row_without_an_artifact_is_rejected():
    problem = _status_problem(99, {"status": "merged", "artifact": None})
    assert problem is not None and "no durable artifact" in problem


def test_a_well_formed_row_is_accepted():
    """Guard the guard: if _status_problem returned a string unconditionally,
    every rejection test above would pass while the manifest was unusable."""
    assert _status_problem(99, {"status": "merged", "artifact": "docs/DEBT.md"}) is None
    assert _status_problem(99, {"status": "open", "artifact": None}) is None


def test_the_manifest_actually_uses_both_statuses():
    """Both branches of the vocabulary are exercised by real rows.

    If every row were `merged`, the open-label check would never run against
    anything and would be dead code reported as coverage.
    """
    statuses = {r["status"] for r in _manifest_rows().values()}
    assert statuses == set(MANIFEST_STATUSES), (
        f"the manifest uses {sorted(statuses)}; both {list(MANIFEST_STATUSES)} "
        f"should appear or one of the checks is never exercised"
    )


def test_an_open_row_may_not_name_an_artifact():
    """The other half of the split the deck now describes.

    `merged` requires a file; `open` requires the absence of one. Only the first
    was enforced, so an open row could have pointed at a path that happened to
    exist and read as landed evidence -- which is precisely the confusion the
    open label exists to prevent.
    """
    problem = _status_problem(28, {"status": "open", "artifact": "docs/DEBT.md"})
    assert problem is not None and "nothing has landed" in problem


@pytest.mark.skipif(not LEGACY_DECK.is_file(), reason="the 2026-08-12 deck is not on this branch")
def test_the_deck_does_not_claim_every_pr_maps_to_a_file():
    """The wording finding, pinned to the deck whose wording was wrong.

    The evidence section said the manifest "maps each one to a file that exists
    in this repository" while carrying an open row with no artifact. A reader
    could come away believing the open maker-checker specification had durable
    repository evidence.

    The forbidden half of this applies to every deck via
    `test_no_deck_claims_every_pr_resolves_to_a_file`; the required phrase is
    this deck's own wording and is not imposed on later ones.
    """
    text = LEGACY_DECK.read_text(encoding="utf-8")
    assert "maps each one to a file that exists" not in text, (
        "the deck claims every cited PR resolves to an existing file, which is "
        "false for any open row"
    )
    assert "no artifact at all" in text, (
        "the deck does not state that an open PR has no landed artifact"
    )


def test_the_manifest_prose_does_not_overclaim_either():
    """The manifest's own introduction is a claim on the same page as the table.

    Correcting the deck left this behind: the manifest still said the resolver
    "asserts every PR reference in the deck appears here with a resolvable
    artifact", directly above a table whose #28 row has none. A reader trusting
    the prose over the table would believe the open specification had landed
    evidence -- which is the whole failure this file was written to stop, made
    by the file itself.
    """
    text = MANIFEST.read_text(encoding="utf-8")
    assert "with a resolvable artifact" not in text, (
        "the manifest claims every cited PR has a resolvable artifact, which is "
        "false for any open row"
    )
    assert "names **no artifact**" in text, (
        "the manifest does not state that an open row has no artifact"
    )


# --- rules that apply to every deck, including the ones not written yet -------
#
# The client's note after the 2026-08-19 demo was "bigger type, fewer words --
# the visuals improved, the reading did not". That is a real requirement and it
# decays silently: nobody notices a slide growing a 30-word bullet until they are
# reading it aloud to a room. So it is asserted, on the bullets that go ON SCREEN
# only -- speaker notes are meant to be prose and are left alone.

#: A projected bullet a presenter can deliver without the room reading ahead.
#:
#: WORDS, not characters, and the unit changed for a reason worth recording.
#: The first version counted characters. Once the measurement was fixed to join
#: wrapped lines (PR #55, BULLET-GUARD-001) it failed five bullets in the
#: 2026-08-12 deck -- but three of those were long only because they cite a file
#: path. A path inside a code span is read as one chunk, not word by word, so a
#: character rule punished exactly the citations these decks exist to carry,
#: while passing a sixteen-word sentence that had no path in it. The client's
#: own phrase was "fewer words".
#:
#: The bound comes from the decks rather than from taste: the 2026-08-20 deck's
#: longest on-screen bullet is 13 words, and the only bullet in either deck above
#: 14 was the one genuinely too long to hear in a single pass.
MAX_ON_SCREEN_WORDS = 14


def _bullet_words(bullet: str) -> int:
    """Words as a listener hears them: a backticked span counts as one.

    `db/tests/test_no_card_data_on_either_schema_path.py` is a single spoken
    token -- "the card-path test" -- not nine words, and emphasis markers are
    formatting rather than speech.
    """
    spoken = re.sub(r"`[^`]+`", "CODE", bullet)
    spoken = re.sub(r"[*_]", "", spoken)
    return len([w for w in spoken.split() if w.strip("—–-")])


def _bullets_from_text(text):
    """Parse `### On screen` bullets out of deck markdown. Pure, so it is testable.

    Returns `(bullet, wrapped)` pairs, where `wrapped` says whether the bullet
    absorbed at least one indented continuation line. The flag exists so the
    joining behaviour can be proved on synthetic input rather than by demanding
    that a real deck carry a long bullet -- see the parser tests below.

    Markdown renders a bullet wrapped across source lines as ONE line on the
    slide, so counting source lines counted the author's text editor rather than
    the projector, and the brevity rule passed the two longest bullets in the
    deck it was written for. Reviewed on PR #55 (BULLET-GUARD-001).

    A non-indented, non-bullet line ENDS the bullet rather than continuing it:
    these decks separate on-screen groups with bold labels like `**Action**`, and
    absorbing one into the bullet above would measure text no slide shows.
    """
    results = []
    for block in text.split("### On screen")[1:]:
        section = block.split("###", 1)[0]
        state = {"current": None, "wrapped": False}

        def flush():
            if state["current"] is not None:
                results.append((state["current"], state["wrapped"]))
            state["current"], state["wrapped"] = None, False

        for raw in section.splitlines():
            line = raw.strip()
            if line.startswith("- "):
                flush()
                state["current"] = line[2:].strip()
            elif state["current"] is None:
                continue
            elif line and raw[:1] in (" ", "	"):
                state["current"] = f"{state['current']} {line}"
                state["wrapped"] = True
            else:
                flush()
        flush()
    return results


def _on_screen_bullets(deck):
    """Bullets under an `### On screen` heading, up to the next `###`.

    Only that section. A deck's notes, evidence tables and question list are
    read, not projected, and holding prose to a slide's word budget would push
    the explanation off the page entirely -- which is the opposite of what the
    feedback asked for.
    """
    return [b for b, _wrapped in _bullets_from_text(deck.read_text(encoding="utf-8"))]


@pytest.mark.parametrize("deck", DECKS, ids=lambda d: d.name)
def test_the_deck_has_on_screen_bullets_to_measure(deck):
    """Guard the guard: a renamed heading would make the length check vacuous."""
    assert _on_screen_bullets(deck), (
        f"{deck.name} has no '### On screen' bullets -- either the heading "
        f"changed or the length rule below is measuring nothing"
    )


@pytest.mark.parametrize("deck", DECKS, ids=lambda d: d.name)
def test_on_screen_bullets_stay_readable_at_a_glance(deck):
    """'Bigger type, fewer words', as an assertion rather than an intention."""
    too_long = [(b, _bullet_words(b)) for b in _on_screen_bullets(deck)
                if _bullet_words(b) > MAX_ON_SCREEN_WORDS]
    assert not too_long, (
        f"{deck.name} has {len(too_long)} on-screen bullet(s) over "
        f"{MAX_ON_SCREEN_WORDS} words. The client asked for bigger type and "
        f"fewer words; a bullet this long is read silently by the room instead "
        f"of heard. Move the detail into the notes:\n  "
        + "\n  ".join(f"({n}w) {b}" for b, n in too_long)
    )


# --- the parser, proved on synthetic markdown --------------------------------
#
# The first attempt at guarding the joining logic asserted that every real deck
# contained a bullet over 78 characters. Review (PR #55, WRAP-GUARD-001) was
# right that it proved the wrong thing in both directions: a long UNWRAPPED line
# satisfies it without joining anything, and a genuinely concise future deck --
# exactly what the brevity rule asks for -- would fail it. The guard and the rule
# it guards pulled in opposite directions, which is a worse defect than the one
# being guarded.
#
# The behaviour belongs to the parser, so it is tested on input written for the
# purpose. No real deck has to carry an artificially long bullet to keep this
# honest.

WRAPPED_FIXTURE = """## Slide 1

### On screen

- A short one
- A bullet that runs past the margin and therefore continues
  onto a second source line
- Another short one

### Notes

- This bullet is under Notes and must not be measured
"""


def test_the_parser_joins_a_wrapped_bullet():
    """The regression proof for BULLET-GUARD-001, on input that isolates it."""
    parsed = _bullets_from_text(WRAPPED_FIXTURE)
    bullets = [b for b, _ in parsed]

    assert len(bullets) == 3, f"wrapping split one bullet into several: {bullets}"
    assert bullets[1] == (
        "A bullet that runs past the margin and therefore continues "
        "onto a second source line"
    )


def test_the_parser_reports_which_bullets_wrapped():
    """The flag has to distinguish, or the test above could pass on a parser
    that joined nothing and simply had three short lines."""
    parsed = _bullets_from_text(WRAPPED_FIXTURE)

    assert [w for _, w in parsed] == [False, True, False]


def test_the_parser_ignores_bullets_outside_the_on_screen_section():
    """Notes are read, not projected. Measuring them would push the explanation
    off the page, which is the opposite of what the feedback asked for."""
    bullets = [b for b, _ in _bullets_from_text(WRAPPED_FIXTURE)]

    assert not any("under Notes" in b for b in bullets)


def test_a_bold_label_ends_the_bullet_rather_than_joining_it():
    """These decks group on-screen bullets under bold labels.

    A label is not a continuation, and absorbing one would measure text that
    appears on no slide -- inflating a bullet the presenter never reads as one.
    """
    parsed = _bullets_from_text(
        "### On screen\n\n- First bullet\n**Action**\n- Second bullet\n"
    )

    assert [b for b, _ in parsed] == ["First bullet", "Second bullet"]
    assert not any(w for _, w in parsed)


def test_a_concise_deck_would_pass_every_rule_here():
    """The contradiction WRAP-GUARD-001 named, pinned so it cannot come back.

    A deck whose bullets are all short must satisfy the brevity rule AND every
    guard around it. The previous version failed exactly this deck.
    """
    concise = "### On screen\n\n- Balance unchanged\n- Approval refused\n"
    bullets = [b for b, _ in _bullets_from_text(concise)]

    assert bullets == ["Balance unchanged", "Approval refused"]
    assert all(_bullet_words(b) <= MAX_ON_SCREEN_WORDS for b in bullets)


@pytest.mark.parametrize("deck", DECKS, ids=lambda d: d.name)
def test_no_deck_claims_every_pr_resolves_to_a_file(deck):
    """The overclaim itself, forbidden everywhere.

    The 2026-08-12 deck's own corrected wording is asserted separately; this is
    the half that must never reappear in any deck, including one written later
    by someone who never read that finding.
    """
    assert "maps each one to a file that exists" not in deck.read_text(encoding="utf-8"), (
        f"{deck.name} claims every cited PR resolves to an existing file, which "
        f"is false for any open row"
    )


# --- commits a deck cites in its own prose ------------------------------------
#
# The manifest's merge commits were checked; a commit written into a deck's
# header or body was not. Review of PR #59 (B2) found the deck claiming it was
# "regenerated from the repository at 92908ce" while its load-bearing claims
# came from two later commits -- so a reader checking provenance landed on a
# tree that did not support what they were reading. The deck failed the exact
# check it teaches people to run.

_DECK_COMMIT = re.compile(r"`([0-9a-f]{7,40})`")


def _deck_commits():
    """(deck, sha) for every commit-shaped citation in a deck's prose."""
    found = []
    for deck in DECKS:
        for raw in _DECK_COMMIT.findall(deck.read_text(encoding="utf-8")):
            # A short sha and a lowercase hex word are the same shape. Require a
            # digit so prose like `deadbeef` or `accede` is not treated as one.
            if any(c.isdigit() for c in raw):
                found.append((deck, raw))
    return found


@pytest.mark.parametrize("deck,sha", _deck_commits(),
                         ids=lambda v: v.name if hasattr(v, "name") else v)
def test_every_commit_a_deck_cites_exists(deck, sha):
    """A provenance line pointing at nothing is worse than none at all.

    Skipped on a shallow clone rather than passed silently -- CI checks out with
    limited history, and a skip that reads like a pass is the failure this whole
    module is about.
    """
    if subprocess.run(["git", "rev-parse", "--is-shallow-repository"],
                      cwd=REPO, capture_output=True, text=True).stdout.strip() == "true":
        pytest.skip("shallow clone -- cited commits are not in this checkout")

    found = subprocess.run(["git", "cat-file", "-e", f"{sha}^{{commit}}"],
                           cwd=REPO, capture_output=True)
    assert found.returncode == 0, (
        f"{deck.name} cites commit {sha}, which is not in this repository"
    )


def test_the_commit_scan_finds_something():
    """Vacuity guard: a regex matching nothing would make the check decorative."""
    assert _deck_commits(), (
        "no deck cites a commit -- either the provenance lines went away or the "
        "pattern stopped matching them"
    )
