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
