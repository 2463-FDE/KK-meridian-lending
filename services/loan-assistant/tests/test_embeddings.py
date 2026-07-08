"""Tests for the local TF-IDF embedder — the free, offline default embedding
provider used so the RAG eval harness never calls a paid API."""
import pytest

from app.embeddings import LocalTfidfEmbedder, apply_idf, build_idf, cosine_similarity


def test_embed_is_deterministic(tmp_path):
    embedder = LocalTfidfEmbedder(cache_path=str(tmp_path / "cache.json"))
    assert embedder.embed("hello world") == embedder.embed("hello world")


def test_embed_caches_by_content_hash(tmp_path):
    cache_path = str(tmp_path / "cache.json")
    embedder = LocalTfidfEmbedder(cache_path=cache_path)
    embedder.embed("some policy text")
    # a fresh embedder pointed at the same cache file should reuse it, not recompute
    reloaded = LocalTfidfEmbedder(cache_path=cache_path)
    assert reloaded._cache == embedder._cache
    assert len(reloaded._cache) == 1


def test_cosine_similarity_identical_vectors_is_one():
    v = {"loan": 2, "amount": 1}
    assert cosine_similarity(v, v) == pytest.approx(1.0)


def test_cosine_similarity_disjoint_vectors_is_zero():
    assert cosine_similarity({"loan": 1}, {"payment": 1}) == 0.0


def test_stopwords_do_not_dominate_similarity(tmp_path):
    """Regression: without stopword filtering, two unrelated sentences that are
    both grammatically ordinary (share "is/the/to/for/a") scored higher than the
    sentence that actually shares the topic words."""
    embedder = LocalTfidfEmbedder(cache_path=str(tmp_path / "cache.json"))
    query = embedder.embed("what is the minimum age to apply for a loan")
    on_topic = embedder.embed("the minimum age to apply is 18 years old")
    off_topic = embedder.embed("this is a policy that applies to the finance charge")
    idf = build_idf([query, on_topic, off_topic])
    q, a, b = apply_idf(query, idf), apply_idf(on_topic, idf), apply_idf(off_topic, idf)
    assert cosine_similarity(q, a) > cosine_similarity(q, b)
