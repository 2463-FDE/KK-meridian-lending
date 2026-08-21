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
import logging
import math
import os
import re
from collections import Counter

log = logging.getLogger("loan-assistant.embeddings")

_DEFAULT_CACHE_PATH = os.path.join(
    os.path.dirname(__file__), "..", ".embedding_cache.json"
)
_TOKEN_RE = re.compile(r"[a-z0-9]+")

# Function words carry no topical signal but appear in nearly every chunk and every
# query -- without filtering them, two unrelated chunks can out-score the right one
# just by both being grammatically ordinary sentences.
#
# Second line added live against the policy-chat feature: a naturally-phrased
# question ("I want to know the NSF fee") failed classify_answerable()'s 0.6
# coverage threshold that the exact same question's terse form ("NSF fee")
# passed -- "want"/"know" never appear in the terse policy text, so they drag
# coverage down even though the actual content term ("nsf") is present and
# correctly retrieved. rag_eval.EVAL_QUERIES was written tersely and never
# exercised this; a live chat gets natural Q&A framing routinely. These are
# common request-framing verbs, not policy vocabulary -- same rationale as the
# grammatical stopwords above, just for conversational rather than grammatical
# filler.
_STOPWORDS = frozenset(
    "a an the is are was were be been being to of in on at for with and or but "
    "this that these those it its as by from what when where which who how do "
    "does did not no if then than so such we you i he she they them our your "
    "want know need tell please explain understand clarify give provide".split()
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
        """Load the cache, treating an unreadable one as empty.

        It is a CACHE: every value in it is recomputable from the text by
        `embed()`, so the correct response to a corrupt file is to rebuild, not
        to fail. Before this, a truncated or double-written file made the
        embedder permanently unconstructible -- and it surfaced as
        `JSONDecodeError: Extra data` from deep inside a caller that had nothing
        to do with caching. Found when the policy tool built an embedder in a
        fresh process and hit a cache file two concurrent test runs had written
        over each other (`_save_cache` is not atomic).

        Not silent: the corruption is logged, because a cache that keeps
        corrupting is a bug worth noticing rather than absorbing forever.
        """
        if not os.path.exists(self.cache_path):
            return {}
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                loaded = json.load(f)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
            log.warning(
                "embedding cache at %s is unreadable (%s) -- rebuilding from "
                "scratch; every entry is recomputable",
                self.cache_path, type(exc).__name__,
            )
            return {}
        if not isinstance(loaded, dict):
            log.warning("embedding cache at %s is not an object -- rebuilding",
                        self.cache_path)
            return {}
        return loaded

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
