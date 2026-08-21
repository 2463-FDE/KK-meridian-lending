"""The one tool the underwriting agent is allowed to call.

The client asked for exactly one bounded, read-only retrieval tool over the
client underwriting policy corpus, invoked by the agent runtime rather than
pre-called in application code. This module is that tool and nothing else.

**What it can reach:** the chunked policy corpus that `corpus.load_policy_corpus`
already produces from `policies/`, filtered to an explicit document allowlist.
That corpus is the one the hygiene gate (ADR 0005) already refuses to load if a
chunk carries PII, so the tool inherits that guarantee rather than restating it.

**What it cannot reach, by construction rather than by instruction:** there is no
SQL here, no shell, no filesystem walk driven by an argument, no URL fetch, no
applicant record, and no write path of any kind. The tool takes a query string
and returns text drawn only from chunks already in memory. A prompt telling the
model to "read /etc/passwd" or "look up applicant 6012" cannot be honoured
because no code path exists to honour it -- which is the only form of injection
resistance worth claiming.

**Bounds are enforced here, not requested of the model.** Top-K and per-excerpt
size are clamped on the way out. A model that asks for 500 results gets
`MAX_TOP_K`; the argument is an input to be validated, not an instruction.

**Provenance travels with every hit** -- document, version, chunk id and a
citation string -- because the summary that follows has to be attributable, and
because the trace (PR B) records those identifiers rather than the text.
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

from pydantic import BaseModel, Field

from .corpus import load_policy_corpus
from .embeddings import LocalTfidfEmbedder, build_idf
from .rag_eval import retrieve

log = logging.getLogger("loan-assistant.policy_tool")

#: Documents the tool may read. An allowlist rather than "everything under
#: policies/", so a file dropped into that directory is not automatically
#: reachable by a model-supplied query. Adding a document is a deliberate edit.
ALLOWED_DOCUMENTS = frozenset({
    "underwriting_guidelines.md",
    "fee_schedule.md",
})

#: Hard ceilings. The model may ask for fewer; it may not ask for more.
MAX_TOP_K = 3
DEFAULT_TOP_K = 3
MAX_EXCERPT_CHARS = 600
MAX_QUERY_CHARS = 300

TOOL_NAME = "search_underwriting_policy"


class PolicyExcerpt(BaseModel):
    """One retrieved chunk, with the provenance a citation needs."""

    document: str
    version: str
    chunk_id: str
    excerpt: str = Field(max_length=MAX_EXCERPT_CHARS)
    citation: str


class PolicyRetrievalResult(BaseModel):
    """What the tool returns to the agent runtime.

    `status` is categorical on purpose: it is the field the trace records, and a
    category carries no client data. "hit"/"miss" is the whole vocabulary.
    """

    status: str
    hit_count: int
    excerpts: list[PolicyExcerpt]


_cache: dict[str, Any] = {}


def _document_version(doc_id: str, text: str) -> str:
    """A content version for a document that carries no version header.

    The corpus is plain Markdown with no front matter, so there is no declared
    version to read. A short content hash is honest: it changes when the
    document changes, it is stable across runs for identical content, and it
    identifies WHICH text was cited without reproducing any of it. Recording a
    hash in a trace leaks nothing; recording the policy text would.
    """
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:12]


def _corpus_state():
    """Allowlisted chunks plus the retrieval index, built once per process.

    Filtered BEFORE the index is built, so a disallowed document does not even
    contribute to the IDF statistics -- it is absent from retrieval rather than
    merely ranked low.
    """
    if not _cache:
        chunks = [c for c in load_policy_corpus() if c["doc_id"] in ALLOWED_DOCUMENTS]
        if not chunks:
            raise RuntimeError(
                "the policy corpus produced no allowlisted chunks -- the tool "
                "would silently return misses for every query"
            )
        versions: dict[str, str] = {}
        by_doc: dict[str, list[str]] = {}
        for chunk in chunks:
            by_doc.setdefault(chunk["doc_id"], []).append(chunk["text"])
        for doc_id, pieces in by_doc.items():
            versions[doc_id] = _document_version(doc_id, "".join(pieces))

        embedder = LocalTfidfEmbedder()
        idf = build_idf([embedder.embed(c["text"]) for c in chunks])
        _cache.update(chunks=chunks, embedder=embedder, idf=idf, versions=versions)
    return _cache["chunks"], _cache["embedder"], _cache["idf"], _cache["versions"]


def reset_cache() -> None:
    """Drop the cached corpus. For tests that vary the corpus on disk."""
    _cache.clear()


def search_underwriting_policy(query: str, top_k: int = DEFAULT_TOP_K) -> dict:
    """Search the client's underwriting policy documents. Read-only.

    Args:
        query: what to look for in the policy documents. Plain text.
        top_k: how many excerpts to return, at most 3.

    Returns a status, a hit count, and up to `top_k` excerpts, each with its
    source document, version and citation.
    """
    # Every bound is applied to what arrived, not to what was asked for. A model
    # is free to send nonsense here; none of it can widen the tool's reach.
    if not isinstance(query, str):
        query = str(query or "")
    query = query.strip()[:MAX_QUERY_CHARS]
    try:
        k = int(top_k)
    except (TypeError, ValueError):
        k = DEFAULT_TOP_K
    k = max(1, min(k, MAX_TOP_K))

    if not query:
        log.info("policy tool: empty query, status=miss")
        return PolicyRetrievalResult(status="miss", hit_count=0, excerpts=[]).model_dump()

    chunks, embedder, idf, versions = _corpus_state()
    hits = retrieve(query, chunks, embedder, idf, k=k)
    hits = [h for h in hits if h.get("score", 0) > 0][:k]

    excerpts = [
        PolicyExcerpt(
            document=h["doc_id"],
            version=versions[h["doc_id"]],
            chunk_id=h["chunk_id"],
            excerpt=h["text"][:MAX_EXCERPT_CHARS],
            # chunk_id already carries the document name ("doc.md#3.0"), so the
            # citation uses it directly -- the first real run produced
            # "underwriting_guidelines.md#underwriting_guidelines.md#7.0".
            citation=f"{h['chunk_id']} ({versions[h['doc_id']]})",
        )
        for h in hits
    ]

    # Categorical only -- count and status, never the query or the text. The
    # same rule the trace follows, applied at the log line so the two cannot
    # disagree about what is safe to retain.
    log.info("policy tool: status=%s hits=%d k=%d",
             "hit" if excerpts else "miss", len(excerpts), k)

    return PolicyRetrievalResult(
        status="hit" if excerpts else "miss",
        hit_count=len(excerpts),
        excerpts=excerpts,
    ).model_dump()
