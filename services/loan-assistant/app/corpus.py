"""Corpus loading + chunking for the policy RAG index.

Only `policies/*.md` is eligible for the corpus. `kb_dump/applications.jsonl` must
never be embedded raw -- it carries unredacted SSN/PAN. See adr/0005 for the corpus
hygiene decision and rag_eval.check_kb_dump_pii() for the offline check that proves it.

Every chunk is redaction-checked before being added -- if the redactor ever flags a
policy doc chunk, that's a real bug (policy docs should be clean) and this refuses to
silently embed it.
"""
import os

from .redactor import redact_str

POLICIES_DIR = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "policies"
)


class CorpusHygieneError(Exception):
    """Raised when a document intended for the corpus contains PII the redactor caught."""


def _chunk(text: str, doc_id: str, max_chars: int = 500) -> list[dict]:
    """Split on blank-line paragraph boundaries, then hard-wrap long paragraphs."""
    paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
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
