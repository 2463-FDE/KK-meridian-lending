# Committed evidence — engineering rule

This directory holds artifacts that a document elsewhere in the repository makes
a claim about: captured telemetry payloads, dependency snapshots, command
output. They are committed so a reader can check a number instead of trusting
one.

**Engineering hygiene rule, not a legal one.** Everything below is how this
repository handles evidence for a **synthetic training engagement**. It is not a
regulatory or legal retention policy, it asserts no retention period, and it must
not be cited as one. Retention, legal holds and deletion obligations for real
data are Week 10 work and remain unauthorised — see `docs/ROADMAP.md` Week 10 and
`docs/DEBT.md`.

## Rules for anything committed here

1. **Privacy-scanned before it is committed.** Not after, and not "it should be
   fine because the exporter has an allowlist". The scan runs against the exact
   bytes going into git.
2. **Allowlisted categorical and provenance fields only.** For trace payloads
   that means the fields `services/loan-assistant/app/trace.py` permits: stage,
   service, status, outcome, role, provider, model family, region, counts,
   tool name, evidence status, document ids and version hashes, citations,
   validator names, refusal class, HTTP status, durations, budgets, tracing
   mode, schema version.
3. **Never committed:** prompts, model responses, retrieved corpus text,
   applicant or customer data, raw financial values, credentials, tokens, PAN,
   CVV, SSN, or any other sensitive content.
4. **If evidence would ever need timed deletion, it does not belong in git.**
   Git history is durable: a later commit removes a file from the tree and not
   from the history, and the content stays reachable by SHA. That is the same
   reasoning `docs/DEBT.md` **D18** records for the untracked log file, and it is
   why "we can delete it later" is not a plan. Evidence with a deletion
   requirement goes somewhere that can honour it, or is not captured.

`db/tests/test_committed_evidence_is_byte_preserved.py` enforces 1–3
mechanically for `*.bin` payloads. Rule 4 is a decision, not a check — nothing
can test it after the fact, which is exactly why it is written down before the
next capture rather than after.

## Byte preservation

`.gitattributes` carries:

```
docs/evidence/**/*.bin -text
```

`core.autocrlf=true` is the Windows default and rewrites line endings on
checkout. Without this rule a committed payload is **not** the bytes that were
captured — measured, not theorised: the first attempt at this directory
committed a 17,443-byte payload that git stored as 17,232. Every byte figure in
the document citing it would have been wrong for anyone who cloned, and the
privacy search would have run over different bytes than the ones captured.

**Scoped to `*.bin` deliberately.** Markdown, JSON, CSV and plain-text evidence
keep normal git text behaviour. A blanket `docs/evidence/** -text` would pin
files nobody has written yet, on a guess about what they need; the narrow rule
covers the case that actually broke and is verified recursively, so the next
dated directory inherits it without anyone remembering to.

## What is here

| Directory | Contents |
|---|---|
| [`2026-08-27-demo-proof/`](2026-08-27-demo-proof/) | The Mode B run recorded in `docs/presentations/2026-08-25-agentic-client-handoff.md` §3a — three trace payloads (20,992 bytes total) and two dependency snapshots |
