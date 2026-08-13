# Evidence manifest — `2026-08-12-three-slides.md`

Every `PR #NN` the deck cites, resolved according to whether it has landed.

A pull request is not durable evidence. It can be renumbered, reopened, closed
without merging, or belong to a fork; a reader offline cannot check any of it;
and a reviewer found exactly this gap — `db/tests/test_presentation_claims_resolve.py`
validated backticked repository paths and let `PR #22`, `PR #23` and `PR #24`
through unchecked, so a wrong number or a quietly reopened PR would still have
passed green.

So the two cases resolve differently, and conflating them is the overclaim this
file exists to prevent:

- a **merged** row names what landed in the repository — a file the audience can
  open and check;
- an **open** row names **no artifact**, because nothing has landed. It carries
  the `open` label instead, on this page and on the slide, and that label is the
  evidence. There is nothing else honest to point at.

`test_presentation_claims_resolve.py` asserts every PR the deck cites appears
here, that a merged row's artifact exists, that an open row has none and is
labelled open on the slide, and that no other status is accepted.

| PR | Status | Durable artifact in this repository | Merge commit |
|---|---|---|---|
| #22 | merged | `services/servicing-service/tests/test_money_routes_require_internal_token.py` | `3551eefd8` |
| #23 | merged | `db/tests/test_readme_schema_claims.py` | `e25bdfa94` |
| #24 | merged | `services/disclosure-service/tests/test_redisplay_is_exact.py` | `2c65c9863` |
| #28 | open | — (nothing has landed; the deck labels this claim open) | — |

## How each column is checked

**Status** is a closed vocabulary: exactly `merged` or `open`. Anything else is
an explicit failure, not a skip.

That is not pedantry. The artifact check handled `merged` and the label check
handled `open`, and each returned early otherwise — so a row typed `landed`,
`merge` or `closed` matched neither branch, was never checked for an artifact,
was never required to carry an open label, and passed green. An unrecognised
value read as "checked" when it meant "skipped", and `landed` is a realistic
typo precisely because the deck uses that word elsewhere.

A PR listed `open` must also be labelled open on the slide — that is the rule
that stopped `specs/0002` being presented as though it had landed.

**Durable artifact** must exist on this branch for every `merged` row. This is
the column that does the real work: it is what a reader can open, and it stays
true after the PR is archived.

An `open` row has no artifact by definition -- that is what open means -- so the
test requires the deck to label the claim as open instead. Letting an open row
name a file that does not exist yet would be the same overclaim in a new
column.

**Merge commit** is asserted when the object is present in the local clone. CI
checks out with limited history, so a shallow clone skips that one assertion
with an explicit reason rather than passing silently — the artifact column is
never skipped, so a row is never accepted on no evidence at all.
