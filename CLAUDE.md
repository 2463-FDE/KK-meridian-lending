# CLAUDE.md

## Scope check — before any reviewer-comment or branch fix

Before touching a single file on a reviewer-comment remediation, a security
fix, or any cross-branch work, run this check first and state the answers
out loud:

1. `git branch --show-current` — which branch am I actually on?
2. If this is for a PR: what is the PR's real base branch (`gh pr view <n>
   --json baseRefName`)? Don't assume `main`.
3. `git log --all --oneline --grep=<keyword>` (and `git branch -a`) — does
   this exact fix already exist on a sibling branch? If yes, propose
   cherry-picking/merging instead of re-implementing it.

Skipping this is what caused the worst repeat friction in this repo: the same
payment-reconciliation fix landed independently on two branches (a 7-file
merge conflict to clean up afterward), and a security fix once got pushed to
the wrong branch entirely, requiring a cherry-pick to correct.

Cheap to run, expensive to skip. Do it first, every time.

## W7–W10 working agreement

### Keep the slicing — it is the thing I fixed
One concern per PR. If a PR title needs "and", it is two PRs. My eight open PRs this week
(#10-#17) average ~285 additions, range 52-598; that is the target, not the exception. The
review said "seven ... ~309", which was true of #10-#16 before this PR existed.

### Estimates get reported against actuals
I estimated 7–10 days for eight items and opened six of them as PRs within two days. Every
estimate I give now gets recorded, and the following Monday I report **actual against estimate**
and say why it moved. Nobody is scored on accuracy — only on whether the gap shrinks and whether
I can explain it.

### Lead time, not just size
My historical median merged lead time is 171 hours — seven days, the worst in the cohort. Size
was never my problem; **sitting** was. A PR that is ready to merge gets merged the same day.

### Every citation resolves
Before I cite a document, it is committed. A citation that does not resolve is a broken claim.

Two examples, both verified rather than assumed. `kal_docs/` holds my private notes,
including a `ROADMAP.md` that is **not** the tracked `docs/ROADMAP.md` — a reviewer already
mistook one for the other, so it is gitignored now. The review copy of this rule cited
`kal_docs/10_WEEK_PLAN.md` as its example — that file has never existed in this repository,
on any branch, so the example itself did not resolve, which is the rule failing on the page
that states it.

The second example has since expired, and saying so is the point. This section used to read
"`docs/ROADMAP.md` currently cites `DEBT.md` D5c, which exists only on PR #16's branch; that
is why #16 merges first." PR #16 merged on 2026-08-10, so D5c is on `main` and that
dependency is gone. A merge-order instruction that outlives its reason is the same defect as
a stale citation: it makes future work carry a constraint nothing enforces any more.

`db/tests/test_docs_citations_resolve.py` now checks this mechanically — every cited path
must resolve, and no document may describe a merged PR as open.
