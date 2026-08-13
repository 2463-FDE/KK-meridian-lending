---
name: verify-regulated-change
description: Verify a compliance, money, or identity claim before it is written down or shipped. Traces claim → source → test → limitation, and refuses claims with no executable evidence. Use when changing or documenting PCI/Reg B/BSA behaviour, money math, KYC, audit trails, or any doc that describes what a control does.
---

# Verify a regulated change

This repository keeps producing one defect in different costumes: **a true-sounding
sentence with nothing enforcing it.** Not sloppiness — each one was written by
someone who had just done the work and described what they meant to be true.

Real instances, all found by review rather than by testing:

| The claim | What was actually there |
|---|---|
| "`payments.pan`/`cvv` are still there, unpurged" | `0031` had dropped them; the README was 2 days stale |
| Policy: "Approve: score ≥ 660 **and DTI ≤ 43%**" | Nothing computed a DTI. No debt figure is collected |
| "reconciliation.peek" as a control | Never scheduled, no threshold, no history, could not fail |
| "the preflight proves the write path" | It wrote a table the money path never touches |
| Runbook: "secrets are in the repo" | `.env` untracked, fallbacks removed months earlier |
| ADR: "one entry per payment" | The index was `(payment_id, component)` — the claim forbade the waterfall |

Each was a *documentation* defect with an *operational* consequence: a denied
applicant reading a criterion nobody evaluated; an operator responding to a
solved incident; a card captured against a credit that could not land.

**This skill is the check that would have caught them.** Run it on any change that
touches money, identity, audit, or a document describing a control.

## Step 0 — inventory every claim the change touches

**Do this before evaluating anything.** The four questions below are useless
applied to a claim you did not know was there, and every defect in the table above
was found in a surface nobody thought to look at: the README two days after the
migration, the FastAPI docstring after the policy was corrected, the runbook months
after the secrets moved.

A claim is any statement about what the system **does**, **prevents**, **stores**,
**checks**, or **guarantees**. Sweep all of these — the ones that bite are the ones
you do not consider part of "the change":

| Surface | Where to look | Why it bites |
|---|---|---|
| **Code** | Branch conditions, constraints, triggers, guards | The claim is what the code *does*, whatever anything says |
| **Tests** | Names and docstrings | A test named `test_pan_is_never_stored` asserts a claim; if it now checks something narrower, the name is a false claim |
| **API descriptions** | Route docstrings, `summary=`, `description=` | **FastAPI publishes docstrings on `/docs`.** Correcting a policy file and leaving the docstring leaves the claim exactly where a caller reads it |
| **Schemas** | Pydantic field comments, `Field(description=)`, examples | The request/response contract; example values imply supported inputs |
| **UI text** | Frontend labels, placeholders, tooltips, dropdown options, error copy | **A placeholder is a published rule.** A reason dropdown offering a criterion nothing evaluates is a false statement to staff and, via adverse-action letters, to applicants |
| **LLM prompts** | System prompts, few-shot examples, output schemas | A prompt instructing a model to apply a rule *generates* that claim on every call. It is not documentation — it is a claim factory |
| **README / runbooks** | Setup, compliance, operational sections | Read during onboarding and during incidents, i.e. when nobody re-derives |
| **ADRs / debt register** | `adr/`, `docs/DEBT.md` | The shared record. A fix leaving a `D`-number stale moved the defect rather than closing it |
| **Policies** | `policies/` | Read by staff and quoted to regulators |
| **Migrations / seeds** | DDL comments, seed data | Seed rows are claims about what valid data looks like |

**How to sweep, concretely.** Grep for the *subject* of the claim across the whole
repository, not the file you edited — the number, the field name, the rule:

```bash
# What you touched -- resolved from the PR's OWN base, not assumed to be main.
# `main...HEAD` is wrong twice over: a PR based on a release branch compares
# against the wrong tree entirely, and a local `main` that has not been fetched
# is stale, which INFLATES the inventory with other people's merged work and
# buries yours.
base_ref=$(gh pr view --json baseRefName -q .baseRefName 2>/dev/null || echo main)
git fetch origin "$base_ref"
merge_base=$(git merge-base "origin/$base_ref" HEAD)
git diff "$merge_base"..HEAD --stat

# Then sweep the whole repository for the SUBJECT of the claim -- the number, the
# field name, the rule -- not just the files above. The surfaces that bite are
# the ones you did not edit.
grep -rniE "dti|43%|debt.to.income" --include="*.py" --include="*.ts"      --include="*.tsx" --include="*.md" --include="*.sql" .
```

`origin/main` is the documented fallback when `gh` is unavailable or the branch
has no PR yet -- and it is a fallback, not the default, because using it on a PR
based on anything else produces an inventory of the wrong change.

Then write the inventory down before judging any of it:

```
| # | Surface | File:line | The claim, as a reader would take it | Changed by this PR? |
```

**Two rules that decide whether the sweep was real:**

1. **A claim you inventoried and did not change still gets the four questions.** If
   the change makes an existing claim false — a schema edit, a retired rule — it is
   in scope regardless of whether you edited that file.
2. **If the inventory has one row, you probably swept one file.** A regulated
   change almost always publishes its claim in three or four places.

## The four questions — applied to EVERY inventoried claim

Answer all four, in order. A claim that cannot answer one is not ready to ship —
and the honest fix is usually to narrow the claim, not to add machinery.

### 1. CLAIM — what exactly is being asserted?

Write it as one falsifiable sentence. If it needs "and", it is two claims and each
gets its own pass.

Rewrite hedges into something checkable. "Handles PII safely" is not a claim.
"`charge()` never writes a PAN to a log line" is.

> Watch for the **weaker-verb slide**: *prevents* → *discourages* → *is designed
> to*. Each step makes the sentence easier to defend and less true.

### 2. SOURCE — where is it enforced, by file and line?

Name the enforcing artifact: the constraint, the trigger, the guard, the branch.

- If the answer is a comment, a docstring or a variable name, **it is not
  enforced**.
- If the answer is "the caller always does X", ask what stops a caller that does
  not.
- If the answer is a `GRANT`/`REVOKE` in this codebase, check it actually holds —
  every service connects as the schema-owning role, so a revoke from the owner
  does not stick (ADR 0002, ADR 0006). This has been assumed wrongly before.

Then check the claim's **scope** against the source's scope. "Money is Decimal"
was true of one read path and false of the other, and the sentence covered both.

### 3. TEST — what fails if the claim stops being true?

Name the test. Then break it on purpose:

```
1. revert the fix (or negate the guard) in the working tree
2. run the focused test
3. confirm it FAILS, and read the failure — it must name the real defect
4. restore. Never commit the mutation.
```

A test that passes with the fix reverted is measuring something else.

Three specific traps, all of which have happened here:

- **The proxy test.** Asserting a header is present rather than that the guard
  ran; probing a table the real path never writes. Test the production path, not
  a similar helper.
- **The hand-maintained list.** "All money routes are guarded" as a literal list
  that reads complete while missing one. Derive the list from the source instead —
  five defects in this repo were the same list-goes-stale shape.
- **The vacuous pass.** A parametrised test over an empty set, a comparison of two
  empty strings, an assertion inside a branch that never runs. Add a
  *guard-the-guard*: assert the fixture found something to check.

For anything touching transactions, migrations, constraints or concurrency, the
test must run against **real PostgreSQL**. A trigger that does not fire and a
CHECK that does not hold are invisible to a mock.

### 4. LIMITATION — what does this NOT do?

State it in the same place as the claim, not in a follow-up ticket. If a reader
could reasonably infer more than is true, the gap is the finding.

Ask specifically:

- **What is out of scope but adjacent?** Removing stored card data is not PCI
  compliance — that needs a QSA, a real processor and a scoped CDE.
- **What is enforced only in the application?** Anyone with direct database access
  usually bypasses it. Say so.
- **What is a contract for a control rather than the control?** An exit code, a
  table row and a metric are what an alerting system consumes — they are not
  alerting.
- **Which environment proved it?** "Works locally" has hidden three CI failures
  here, all because a developer `.env` supplied what a clean checkout does not.

## Before you write the sentence

- [ ] Does the claim already exist elsewhere, stated differently? Two versions of
      one fact drift, and the copy nobody applies is the copy nobody notices is
      wrong.
- [ ] Does any **API-facing** text repeat it? FastAPI serves docstrings on `/docs`
      — correcting a policy file while leaving the docstring leaves the claim
      reachable exactly where a caller looks.
- [ ] Does the debt register agree? `docs/DEBT.md` is the shared record; a fix that
      leaves a `D`-number stale has moved the defect rather than closed it.
- [ ] If this claim is retired, is the **history** kept? Deleting an
      acknowledged gap makes it invisible, which is worse than the overclaim.

## Grading what you find

| Verdict | Meaning |
|---|---|
| **Done** | Claim enforced, test fails when reverted, limitation stated, evidence linked |
| **Partial** | Enforced but incomplete, or evidence exists and the control does not close the risk |
| **Decision required** | Two defensible answers with different product or compliance consequences. Do **not** pick silently — present both with cost |
| **Not started** | No implementation. A design document is not an implementation |

`Partial` is the honest verdict most often, and the one most often skipped.

## Output

Report per claim, in this shape:

```
INVENTORY   N claims found across M surfaces (list them, with file:line)

then, per claim:

CLAIM       one falsifiable sentence
SOURCE      file:line of the thing that enforces it
TEST        test name + the mutation result that proves it bites
LIMITATION  what a reader might infer that is not true
VERDICT     Done | Partial | Decision required | Not started
```

Report the inventory even when a surface came back empty — "no UI text publishes
this" is a finding a reader can check, and "I did not look at the UI" is not.

If the mutation did not fail, say so and stop. An unproven claim reported as
verified is the defect this skill exists to prevent, committed by the tool meant
to catch it.
