# Evidence manifest — `2026-08-12-three-slides.md`

Every `PR #NN` the deck cites, resolved to something that survives the PR.

A pull request is not durable evidence. It can be renumbered, reopened, closed
without merging, or belong to a fork; a reader offline cannot check any of it;
and a reviewer found exactly this gap — `db/tests/test_presentation_claims_resolve.py`
validated backticked repository paths and let `PR #22`, `PR #23` and `PR #24`
through unchecked, so a wrong number or a quietly reopened PR would still have
passed green.

So each row names **what landed in the repository**, which is what the audience
can actually verify, and `test_presentation_claims_resolve.py` asserts every PR
reference in the deck appears here with a resolvable artifact.

| PR | Status | Durable artifact in this repository | Merge commit |
|---|---|---|---|
| #22 | merged | `services/servicing-service/tests/test_money_routes_require_internal_token.py` | `3551eefd8` |
| #23 | merged | `db/tests/test_readme_schema_claims.py` | `e25bdfa94` |
| #24 | merged | `services/disclosure-service/tests/test_redisplay_is_exact.py` | `2c65c9863` |
| #28 | open | — (nothing has landed; the deck labels this claim open) | — |

## How each column is checked

**Status** must agree with how the deck labels the claim. A PR listed `open`
here has to be labelled as open on the slide too — that is the rule that stopped
`specs/0002` being presented as though it had landed.

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
