# ADR 0005: RAG corpus hygiene — what's embeddable, what isn't, and the decision-record gap it exposes

- **Status:** Accepted
- **Date:** 2026-07-08
- **Author:** In-house team

## Context

Dana asked for a helper that answers underwriting-policy questions from the
`policies/` docs and past decisions, handing over the `policies/` folder plus
`kb_dump/applications.jsonl` "for context." An officer tried "why was application
#6012 denied?" and got zero results back.

Before building retrieval, two things needed checking: what's actually safe to put
in a vector store, and whether the empty result was a retrieval bug or something
else.

`kb_dump/applications.jsonl` (6 records) carries raw `ssn` and `pan` fields,
unredacted, on every record. `decisions` (`db/init/001_schema.sql`) is `(app_id,
outcome)` — no reason, no score, no timestamp, for any application, ever (RF-18).
Application `#6012` (Travis Booker, `deny`) exists in the JSONL with no explanation
field anywhere in the system.

## Decision

- **Corpus scope:** only `policies/*.md` is eligible for embedding. `kb_dump/`
  (and any future raw-application export) is never embedded — the redactor from
  Week 1 (`app/redactor.py`, Luhn-validated PAN + SSN detection) runs as an offline
  regex gate on ingest (`app/corpus.py::load_policy_corpus`), and refuses to add any
  chunk it flags. This is checked with a plain regex pass, not an LLM call, per the
  quota note in the brief.
- **Retrievable decision-record requirement:** "why was X denied" cannot be answered
  by any retrieval design over the current schema, because the fact doesn't exist
  yet. Before this feature can answer applicant-specific questions, `decisions` (or
  a successor table — see Week 3's planned `decision_events`) needs to persist a
  real reason per decision. Until then, the eval harness's job is to prove that gap
  explicitly rather than let the feature silently return nothing or, worse,
  hallucinate a plausible-sounding reason from the policy text.
- **Retrieval implementation:** a free, local, deterministic TF-IDF cosine-similarity
  retriever (`app/embeddings.py`), not a paid embeddings API. The corpus is 68 lines
  across 2 files — nowhere near large enough to justify per-call embedding cost, and
  the brief explicitly asks to keep cost low on a Pro-tier plan. Vectors are cached
  on disk keyed by content hash so re-running the eval harness never re-embeds the
  same chunk twice. The embedder is written behind a small interface so a real
  provider (Voyage AI, OpenAI) can be substituted later without touching the
  retrieval or eval code — same abstraction shape as the `CreditBureauClient`
  pattern planned for `decision-service` (RF-21).

## Consequences

- **Pro:** no PII can enter the vector store by construction, not by discipline —
  the gate raises `CorpusHygieneError` if a chunk ever fails redaction, rather than
  silently redacting and continuing.
- **Pro:** the eval harness (`app/rag_eval.py`) proves both gaps explicitly in its
  report — PII found in `kb_dump` (named fields), and the missing-decision-record
  gap (RF-18) — instead of just returning search results and letting the real
  problem hide behind "0 results found."
- **Con — the feature is not fully useful yet.** Even with perfect retrieval over
  `policies/`, "why was #6012 denied" still can't be answered, because the data to
  answer it doesn't exist. This ADR does not fix that; it documents the requirement
  (a retrievable decision-record) as a prerequisite for a later week.
- **Con — local TF-IDF is a real but limited retriever.** It found every fact in this
  week's eval query set correctly once stopwords were filtered and IDF weighting
  was added, but a corpus this small doesn't stress-test recall/precision the way a
  larger one would. If the policy corpus grows significantly, a real embedding
  model should be evaluated against this same harness before assuming TF-IDF still
  suffices.
