"""Tests for the Week 2 RAG retrieval eval harness.

Covers the two things the deliverable has to prove:
  (a) kb_dump/applications.jsonl is unsafe to embed raw (PII check)
  (b) retrieval against the policy corpus is accurate, including on the two cases
      where the correct answer is "the corpus has no data for this"
"""
from app.embeddings import LocalTfidfEmbedder, build_idf
from app.corpus import load_policy_corpus
from app.rag_eval import EVAL_QUERIES, check_kb_dump_pii, retrieve, run_eval


def test_kb_dump_pii_check_finds_ssn_and_pan():
    result = check_kb_dump_pii()
    assert result["checked"] is True
    assert result["record_count"] >= 1
    assert "ssn" in result["pii_fields_found"]
    assert "pan" in result["pii_fields_found"]
    assert result["safe_to_embed_raw"] is False


def test_retrieve_finds_the_right_chunk_for_a_known_fact():
    chunks = load_policy_corpus()
    embedder = LocalTfidfEmbedder()
    idf = build_idf([embedder.embed(c["text"]) for c in chunks])
    hits = retrieve("what is the late fee amount", chunks, embedder, idf)
    assert hits
    assert "$35" in hits[0]["text"]


def test_run_eval_all_queries_correct():
    result = run_eval()
    assert result["all_queries_correct"] is True
    assert len(result["query_results"]) == len(EVAL_QUERIES)


def test_run_eval_surfaces_missing_decision_record_gap():
    result = run_eval()
    assert "RF-18" in result["findings"]["missing_decision_record_gap"]


def test_run_eval_surfaces_pii_in_corpus_source():
    result = run_eval()
    finding = result["findings"]["pii_in_corpus_source"]
    assert finding is not None
    assert "ssn" in finding and "pan" in finding
