"""Tests for the Week 2 RAG retrieval eval harness.

Covers the two things the deliverable has to prove:
  (a) kb_dump/applications.jsonl is unsafe to embed raw (PII check)
  (b) retrieval against the policy corpus is accurate, including on the two cases
      where the correct answer is "no answer" -- classified by classify_answerable()
      against what retrieve() actually returns, not by checking corpus-wide ground
      truth (that was the gap Codex review found on this PR: retrieve() still hands
      back a real, topically-related chunk with a real score for these queries, so
      "correct" has to reflect an actual no-answer gate in the pipeline, not just a
      test-time fact about the corpus).
"""
from app.embeddings import LocalTfidfEmbedder, build_idf
from app.corpus import load_policy_corpus
from app.rag_eval import EVAL_QUERIES, check_kb_dump_pii, classify_answerable, retrieve, run_eval


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


def test_denial_reason_query_classified_as_no_answer_not_just_missing_id():
    """Regression (Codex review on this PR): retrieve() still returns a real chunk
    with a real, nontrivial score for this query (the general Reg B adverse-action
    policy section, which genuinely shares vocabulary with "denied"/"application").
    The old version of this harness only checked that the literal string "6012"
    wasn't anywhere in the corpus and called that "correct" -- true, but it didn't
    prove the actual retrieval path would refuse to hand that chunk to an answer
    generator. This asserts the real classification the pipeline produces."""
    result = run_eval()
    denial_result = next(
        r for r in result["query_results"] if "6012" in r["query"]
    )
    assert denial_result["status"] == "no_answer"
    assert denial_result["top_score"] > 0.05, (
        "this must be a real, non-trivial hit -- the point is that a decent score "
        "alone isn't treated as answerable without the grounding fact present"
    )


def test_classify_answerable_rejects_high_score_hit_missing_the_fact():
    """A hit can score well on generic topical overlap while still not containing
    the specific fact needed -- classify_answerable must reject it anyway."""
    hits = [{"chunk_id": "x", "score": 0.9, "text": "denied applications get an adverse action notice"}]
    assert classify_answerable(hits, "6012") is False


def test_classify_answerable_accepts_a_grounded_hit():
    hits = [{"chunk_id": "x", "score": 0.5, "text": "minimum age: 18"}]
    assert classify_answerable(hits, "18") is True


def test_classify_answerable_rejects_empty_hits():
    assert classify_answerable([], "18") is False
