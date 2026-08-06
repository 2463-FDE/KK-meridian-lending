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
One concern per PR. If a PR title needs "and", it is two PRs. My seven open PRs this week average
~309 additions; that is the target, not the exception.

### Estimates get reported against actuals
I estimated 7–10 days for eight items and opened six of them as PRs within two days. Every
estimate I give now gets recorded, and the following Monday I report **actual against estimate**
and say why it moved. Nobody is scored on accuracy — only on whether the gap shrinks and whether
I can explain it.

### Lead time, not just size
My historical median merged lead time is 171 hours — seven days, the worst in the cohort. Size
was never my problem; **sitting** was. A PR that is ready to merge gets merged the same day.

### Every citation resolves
My ADRs cite a `ROADMAP.md` and a `kal_docs/10_WEEK_PLAN.md` that are **not in the repository**.
Before I cite a document, it is committed. A citation that does not resolve is a broken claim.
