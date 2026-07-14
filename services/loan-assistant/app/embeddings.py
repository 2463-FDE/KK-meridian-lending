"""Embedding provider abstraction for RAG retrieval.

Defaults to a free, local, deterministic embedder (bag-of-words TF cosine similarity)
so the eval harness runs entirely offline and never touches a paid API or spends a
token on embeddings. Swap in a real provider (Voyage AI, OpenAI) later by implementing
the same embed() interface -- mirrors the CreditBureauClient abstraction pattern.

Vectors are cached on disk keyed by content hash so re-running the harness never
re-embeds the same chunk twice (Week 2 quota constraint: never re-embed per run).
"""
import hashlib
import json
import math
import os
import re
from collections import Counter

_DEFAULT_CACHE_PATH = os.path.join(
    os.path.dirname(__file__), "..", ".embedding_cache.json"
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Function words carry no topical signal but appear in nearly every chunk and every
# query -- without filtering them, two unrelated chunks can out-score the right one
# just by both being grammatically ordinary sentences.
_STOPWORDS = frozenset(
    "a an the is are was were be been being to of in on at for with and or but "
    "this that these those it its as by from what when where which who how do "
    "does did not no if then than so such we you i he she they them our your".split()
)


def tokenize(text: str) -> list[str]:
    return [t for t in _TOKEN_RE.findall(text.lower()) if t not in _STOPWORDS]


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class LocalTfidfEmbedder:
    """Free, local, deterministic embedder. No external API calls, no API cost."""

    def __init__(self, cache_path: str = _DEFAULT_CACHE_PATH):
        self.cache_path = cache_path
        self._cache = self._load_cache()

    def _load_cache(self) -> dict:
        if os.path.exists(self.cache_path):
            with open(self.cache_path, encoding="utf-8") as f:
                return json.load(f)
        return {}

    def _save_cache(self) -> None:
        with open(self.cache_path, "w", encoding="utf-8") as f:
            json.dump(self._cache, f)

    def embed(self, text: str) -> dict:
        """Return a term-frequency vector, cached by content hash."""
        key = _content_hash(text)
        if key in self._cache:
            return self._cache[key]
        vec = dict(Counter(tokenize(text)))
        self._cache[key] = vec
        self._save_cache()
        return vec


def build_idf(vectors: list) -> dict:
    """Inverse document frequency over a fixed set of term-frequency vectors.
    Smoothed (log((n+1)/(df+1)) + 1) so a term appearing in every doc doesn't hit
    zero weight and a term absent from the corpus doesn't divide by zero."""
    n = len(vectors)
    df = Counter()
    for vec in vectors:
        for term in vec:
            df[term] += 1
    return {term: math.log((n + 1) / (freq + 1)) + 1 for term, freq in df.items()}


def apply_idf(vec: dict, idf: dict) -> dict:
    """Weight a raw term-frequency vector by corpus IDF -- this is the 'IDF' half
    of TF-IDF. Without it, common words (shared across every doc) dominate cosine
    similarity as much as the words that actually distinguish one chunk from another."""
    return {term: freq * idf.get(term, 0.0) for term, freq in vec.items()}


def cosine_similarity(vec_a: dict, vec_b: dict) -> float:
    common = set(vec_a) & set(vec_b)
    dot = sum(vec_a[t] * vec_b[t] for t in common)
    mag_a = math.sqrt(sum(v * v for v in vec_a.values()))
    mag_b = math.sqrt(sum(v * v for v in vec_b.values()))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)
