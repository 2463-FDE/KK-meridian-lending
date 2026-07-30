# ADR 0007: Underwriting policy names DTI and fraud-flag cutoffs the code never implements

- **Status:** Accepted
- **Date:** 2026-07-15
- **Author:** In-house team

## Context

Discovered live, through the new policy-chat feature: a staff member asked
"what is the policy" for the decisioning criteria and got back
`underwriting_guidelines.md#5.0`, quoted here verbatim since the exact wording
matters:

> 1. Pull credit (Experian) and obtain a score. 2. Run the risk model to
> produce a model score (0–850 scale) and a decision band. 3. Apply policy
> cutoffs: **Approve:** model score ≥ 660 and DTI ≤ 43%. **Refer (manual
> review):** model score 600–659, or DTI 43–50%. **Deny:** model score < 600,
> or DTI > 50%, or fraud flag. 4. Counteroffer is permitted (lower amount /
> shorter term) when score is in the refer band.

The score cutoffs (≥660 / 600–659 / <600) match `decision-service/app/
decision.py::_run_model()` exactly. The DTI and fraud-flag criteria do not
match anything in the running system — verified directly, not assumed:

- `grep -rni "dti\|debt.to.income\|fraud" services/decision-service/app/*.py`
  returns **zero matches**. `decide()`/`_run_model()` compute `model_score`
  from `bureau_score` and `income` only (the same "2-input model" Week 3/
  Week 8 already found — this ADR adds that the *policy itself* claims two
  more inputs the model was never given at all).
- `origination-service/app/routers/applications.py:182` sends
  `"monthly_debt": 0` to `decision-service` on every request, with its own
  comment: `# not captured in the LOS today`.
- `db/init/001_schema.sql` has **no debt/monthly-debt column anywhere** in
  `applications` — this isn't unwired application code sitting on top of
  available data, the schema itself has never had a place to store an
  applicant's existing monthly debt obligations. Same class of gap as
  `kyc_checks` never having a sanctions-screen column: the capability was
  never scoped, not merely left unconnected.
- No fraud-flag mechanism exists anywhere in this codebase — no column, no
  check function, no external signal source. "Deny on fraud flag" has no
  code path that could ever fire it.

Consequence: every real decision this system makes is evaluated against
**two of the policy's four stated criteria** (bureau-derived score cutoffs
only). DTI and fraud are named in the document a denied applicant or a
regulator would be shown as "the policy," and neither was ever actually
checked for any application, ever. This is a sharper version of Week 8's
"2-input model" finding: it's not only that the licensed model has two
inputs — the *written policy the model is supposed to implement* names two
additional decision criteria that no part of the system, model included, is
capable of evaluating today.

## Decision

Record this gap formally rather than silently coding an assumed DTI/fraud
implementation. A real one needs product/compliance sign-off this ADR
cannot supply on its own — which existing obligations count toward DTI, how
they'd be collected/verified, and what actually constitutes a "fraud flag"
(an external signal? a rules-based heuristic? which one?) are business
decisions, not something to invent unilaterally in code.

Two mismatches on record, explicitly:

1. **DTI.** Policy states cutoffs at 43%/50%. No computation path exists:
   no schema column for existing debt, the LOS intake form never asks for
   it, and `origination-service` hardcodes the one field that would carry it
   to `0` before decision-service ever sees it.
2. **Fraud flag.** Policy states a deny condition. No fraud-detection
   mechanism — rules-based or vendor-sourced — exists anywhere in the
   codebase to produce that flag.

Until either is built: `decision_events.top_features`/`reason_codes` (Week 3,
`db/init/004_decision_events.sql`) must be read as covering **only** the two
dimensions this system can actually evaluate today — bureau score and
income — not as evidence that DTI or fraud were considered for a given
decision, regardless of what `underwriting_guidelines.md` implies every
decision checks.

**Not built as part of this ADR** — both are real, separate efforts out of
scope here: DTI needs a schema change plus an intake-form field plus a
product/compliance-approved definition of what counts as debt; fraud
detection needs an actual signal source, not a stub that would just create
the same false-confidence problem this ADR is naming. This ADR is the
record that the gap exists and is understood — matching this repo's
existing convention (ADR 0006's sync→async note, ADR 0004's "the debt
moved, it did not get fixed") of writing down a deferred problem explicitly
rather than letting it stay implicit.

## Consequences

- **Pro:** a real, previously undocumented gap between written policy and
  enforced policy is now on record in `adr/`, not something living only in
  application code line-by-line where nobody would think to look for it.
- **Pro:** strengthens, alongside Week 8's model-card and fairness-
  monitoring findings, the case that "wider rollout" of the AI scorer is
  premature — the system doesn't yet enforce the criteria its own published
  underwriting policy already claims to apply.
- **Con:** until fixed, every `decision_events` row's `reason_codes`/
  `top_features` implicitly overstate what was actually checked, relative to
  what `underwriting_guidelines.md` tells a reader — including a denied
  applicant or a regulator — is checked for every application. This ADR
  does not fix that overstatement; it names it precisely so it can't be
  mistaken for something already handled.
- **Con:** no DTI/fraud implementation plan is specified beyond what's
  needed structurally (a schema change + intake field; a real fraud-signal
  source) — deliberately, since guessing the specific business rules here
  without product/compliance input would repeat the exact mistake (an
  under-specified control shipped as if it were a real one) this ADR exists
  to stop happening again.
