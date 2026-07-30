"""Tests for corpus loading + the redaction gate on ingest.

Week 2 deliverable: a redaction/PII check that runs before anything enters the
retrieval corpus, offline (regex via the Week 1 redactor), not an LLM call.
"""
import pytest

from app.corpus import CorpusHygieneError, load_policy_corpus


def test_real_policy_docs_load_clean():
    """The actual policies/ docs must pass the redaction gate — they're supposed
    to be clean; this is the regression test that keeps them that way."""
    chunks = load_policy_corpus()
    assert len(chunks) > 0
    for chunk in chunks:
        assert "doc_id" in chunk and "chunk_id" in chunk and "text" in chunk


def test_corpus_refuses_a_doc_with_pii(tmp_path):
    bad_dir = tmp_path / "policies"
    bad_dir.mkdir()
    (bad_dir / "leaky.md").write_text(
        "# Leaky Policy\n\nApplicant SSN 123-45-6789 was used as an example here.",
        encoding="utf-8",
    )
    with pytest.raises(CorpusHygieneError):
        load_policy_corpus(policies_dir=str(bad_dir))


def test_corpus_allows_a_clean_doc(tmp_path):
    clean_dir = tmp_path / "policies"
    clean_dir.mkdir()
    (clean_dir / "clean.md").write_text(
        "# Clean Policy\n\nMinimum loan amount is $1,000.", encoding="utf-8"
    )
    chunks = load_policy_corpus(policies_dir=str(clean_dir))
    assert any("1,000" in c["text"] for c in chunks)


def test_consecutive_headers_are_not_silently_dropped(tmp_path):
    """Review finding: the first version of the header-merge fix tracked a
    single pending_header scalar, so a header immediately followed by another
    header (no content between them) silently overwrote and discarded the
    first one -- it never appeared in any chunk, with no error."""
    doc_dir = tmp_path / "policies"
    doc_dir.mkdir()
    (doc_dir / "nested.md").write_text(
        "## Section One\n\n### Subsection\n\nActual content lives here.",
        encoding="utf-8",
    )
    chunks = load_policy_corpus(policies_dir=str(doc_dir))
    all_text = "\n".join(c["text"] for c in chunks)
    assert "Section One" in all_text
    assert "Subsection" in all_text
    assert "Actual content lives here" in all_text
