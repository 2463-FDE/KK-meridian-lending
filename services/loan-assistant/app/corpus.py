"""Corpus loading + chunking for the policy RAG index.

Only `policies/*.md` is eligible for the corpus. `kb_dump/applications.jsonl` must
never be embedded raw -- it carries unredacted SSN/PAN. See adr/0005 for the corpus
hygiene decision and rag_eval.check_kb_dump_pii() for the offline check that proves it.

Every chunk is redaction-checked before being added -- if the redactor ever flags a
policy doc chunk, that's a real bug (policy docs should be clean) and this refuses to
silently embed it.
"""
import os
import re

from .redactor import redact_str

_HEADER_RE = re.compile(r"^#{1,6}\s+.+$")

# Default assumes a local checkout (services/loan-assistant/app -> repo root ->
# policies/). Override with POLICIES_DIR in any environment where that relative
# layout doesn't hold -- confirmed live: it doesn't inside this service's own
# Docker image (WORKDIR /app, COPY app ./app means the same "..","..",".." only
# walks up to filesystem root, not the repo root), so docker-compose.yml sets
# POLICIES_DIR explicitly alongside the ./policies volume mount.
POLICIES_DIR = os.getenv(
    "POLICIES_DIR",
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "policies"),
)


class CorpusHygieneError(Exception):
    """Raised when a document intended for the corpus contains PII the redactor caught."""


def _chunk(text: str, doc_id: str, max_chars: int = 500) -> list[dict]:
    """Split on blank-line paragraph boundaries, then hard-wrap long paragraphs.

    A lone markdown header paragraph gets merged into the paragraph that follows
    it rather than emitted as its own chunk. Confirmed live: a blank line between
    a "## Section" heading and its content (standard markdown, both policy docs
    do this everywhere) made the header its own content-free chunk -- asking
    about a section by its heading name ("Eligibility", "Credit decisioning")
    retrieved that bare heading as the top hit instead of the actual bullet list
    right after it, since the content paragraph never repeats the heading word.

    Review finding on the first version of this fix: it tracked a single
    `pending_header` scalar, so two consecutive headers (e.g. an "## H2"
    immediately followed by an "### H3" before any body text) silently dropped
    the first one -- it was overwritten, never appended anywhere, with no error.
    Fixed by accumulating a list of pending headers instead of one scalar, so
    any run of consecutive headers merges together with whatever content
    eventually follows (or with each other, if a header sits at end of file).
    """
    raw_paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    paragraphs: list[str] = []
    pending_headers: list[str] = []
    for para in raw_paragraphs:
        if _HEADER_RE.match(para) and "\n" not in para:
            pending_headers.append(para)
            continue
        if pending_headers:
            para = "\n".join(pending_headers + [para])
            pending_headers = []
        paragraphs.append(para)
    if pending_headers:
        # Trailing header(s) with nothing after them (end of file) -- keep
        # them rather than silently dropping them.
        paragraphs.append("\n".join(pending_headers))

    chunks = []
    for i, para in enumerate(paragraphs):
        for j in range(0, len(para), max_chars):
            piece = para[j : j + max_chars]
            chunks.append(
                {"doc_id": doc_id, "chunk_id": f"{doc_id}#{i}.{j // max_chars}", "text": piece}
            )
    return chunks


def load_policy_corpus(policies_dir: str = POLICIES_DIR) -> list[dict]:
    """Load + chunk the policy docs. Raises CorpusHygieneError if any chunk fails
    the redaction check -- policy docs are expected to be clean, so a failure here
    means investigate before ingesting, not silently redact and continue."""
    chunks = []
    for fname in sorted(os.listdir(policies_dir)):
        if not fname.endswith(".md"):
            continue
        path = os.path.join(policies_dir, fname)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        for chunk in _chunk(text, doc_id=fname):
            safe = redact_str(chunk["text"])
            if safe != chunk["text"]:
                raise CorpusHygieneError(
                    f"Redactor caught PII in {chunk['chunk_id']} -- refusing to add "
                    "it to the corpus. Policy docs should be clean; investigate "
                    "before ingesting."
                )
            chunks.append(chunk)
    return chunks
