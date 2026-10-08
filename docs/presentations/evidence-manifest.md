# Evidence manifest — every deck in this folder

Every `PR #NN` any deck in `docs/presentations/` cites, resolved according to
whether it has landed. One manifest rather than one per deck: two decks citing
the same PR must not be able to disagree about what it is.

A pull request is not durable evidence. It can be renumbered, reopened, closed
without merging, or belong to a fork; a reader offline cannot check any of it;
and a reviewer found exactly this gap — `db/tests/test_presentation_claims_resolve.py`
validated backticked repository paths and let `PR #22`, `PR #23` and `PR #24`
through unchecked, so a wrong number or a quietly reopened PR would still have
passed green.

Each row carries **two statuses**, because a dated deck is a historical snapshot
and the repository has moved on since:

- **Status at presentation** is what the PR was on the date of the deck that cites
  it. The decks are not rewritten, so this column is what they say.
- **Current repository status** is what the PR is now, verified against GitHub
  (last checked 2026-10-07).

The two cases still resolve differently, and conflating them is the overclaim
this file exists to prevent:

- a **merged** row names what landed in the repository — a file the audience can
  open and check;
- a row whose current status is **open** names **no artifact**, because nothing
  has landed. It carries the `open` label instead, and that label is the
  evidence. There is nothing else honest to point at.

`test_presentation_claims_resolve.py` asserts every PR the decks cite appears
here, that a currently merged row's artifact exists, that a currently open row
has none, that a PR which was open at presentation time is still labelled open
on the slide that cited it (the slide is not retro-edited), and that no other
status is accepted.

| PR | Status at presentation | Current repository status | Durable artifact in this repository | Merge commit |
|---|---|---|---|---|
| #22 | merged | merged | `services/servicing-service/tests/test_money_routes_require_internal_token.py` | `3551eefd8` |
| #23 | merged | merged | `db/tests/test_readme_schema_claims.py` | `e25bdfa94` |
| #24 | merged | merged | `services/disclosure-service/tests/test_redisplay_is_exact.py` | `2c65c9863` |
| #28 | open | merged | `specs/0002-maker-checker-self-approval.md` | `bdac3f69c` |
| #50 | merged | merged | `scripts/check_self_approval.sh` | `13eb622d9` |
| #51 | merged | merged | `services/payment-service/tests/test_pan_cvv_never_enter_the_payment_path.py` | `92908ce0b` |
| #52 | merged | merged | `services/servicing-service/tests/test_double_capture_is_not_detected_yet.py` | `1ecb74e8e` |
| #53 | merged | merged | `db/tests/test_maker_checker_limits_have_one_source.py` | `48b5283bc` |
| #56 | merged | merged | `services/payment-service/tests/test_correlation_id_survives_the_payment_path.py` | `81c16bb74` |
| #58 | merged | merged | `services/servicing-service/tests/test_payment_allocation_is_read_from_the_ledger.py` | `ee8f733c4` |

**PR #28** ("Spec 0002: maker-checker for servicing money movements") was open when
the 2026-08-12 deck was presented, and that deck still says so. It merged on
2026-08-13 as `bdac3f69c`; the specification is now on `main`.

## How each column is checked

**Both status columns** are a closed vocabulary: exactly `merged` or `open`.
Anything else is an explicit failure, not a skip.

That is not pedantry. The artifact check handled `merged` and the label check
handled `open`, and each returned early otherwise — so a row typed `landed`,
`merge` or `closed` matched neither branch, was never checked for an artifact,
was never required to carry an open label, and passed green. An unrecognised
value read as "checked" when it meant "skipped", and `landed` is a realistic
typo precisely because the deck uses that word elsewhere.

A PR that was `open` at presentation time must still be labelled open on the
slide that cited it — that is the rule that stopped `specs/0002` being presented
as though it had landed, and it also stops the slide being edited after the fact
to look as if it always had.

**Durable artifact** must exist on this branch for every row whose current status
is `merged`. This is
the column that does the real work: it is what a reader can open, and it stays
true after the PR is archived.

A currently `open` row has no artifact by definition -- that is what open means -- so the
test requires the deck to label the claim as open instead. Letting an open row
name a file that does not exist yet would be the same overclaim in a new
column.

**Merge commit** is asserted when the object is present in the local clone. CI
checks out with limited history, so a shallow clone skips that one assertion
with an explicit reason rather than passing silently — the artifact column is
never skipped, so a row is never accepted on no evidence at all.
