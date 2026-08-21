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
import tempfile
from collections import Counter

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
        """Read the cache, treating an unreadable one as absent.

        Every value here is a term-frequency vector recomputable from the text
        that produced it, so a cache that cannot be read costs time and nothing
        else. Failing the caller instead would turn a disposable optimisation
        into an outage -- which is exactly what happened before the write below
        was made atomic.
        """
        if not os.path.exists(self.cache_path):
            return {}
        try:
            with open(self.cache_path, encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return {}

    def _save_cache(self) -> None:
        """Write the cache atomically.

        The previous version opened the real path with "w", which truncates
        immediately and serialises afterwards. Any reader arriving inside that
        window got an empty or half-written file and raised JSONDecodeError.

        That was latent until the underwriting agent shipped: retrieval used to
        run once, server-side, in sequence. The agent's model turn emits several
        tool calls at once and LangGraph runs them in parallel threads, each
        constructing an embedder that loads and rewrites this same file. Proven
        rather than theorised -- eight concurrent embedders reproduce it, and it
        took down the containerised summary end to end, surfacing as
        `AgentProviderError (JSONDecodeError)` after two successful tool calls.

        `os.replace` is atomic on POSIX and on Windows, so a reader sees either
        the old file or the new one and never a partial write. The temporary
        file is created in the same directory because a cross-filesystem replace
        is not atomic.

        Two writers can still each persist their own full dict, so one may
        overwrite entries the other added. That is a lost cache entry, not a
        lost result: the next `embed()` recomputes it. Locking to prevent it
        would add contention to fix nothing a user could observe.

        **A snapshot is serialised, not the live dict.** `policy_tool` memoises
        one embedder per process, so parallel tool calls share this instance and
        this dictionary -- one thread inserting in `embed()` while another
        iterates it inside `json.dump` raises "dictionary changed size during
        iteration". That is a second, separate race from the truncated file
        above, and it was caught by CI rather than locally: the timing window is
        narrow enough that this machine never hit it. `dict(...)` copies under
        the GIL without releasing it, so the copy itself cannot tear.
        """
        directory = os.path.dirname(os.path.abspath(self.cache_path)) or "."
        snapshot = dict(self._cache)
        try:
            fd, tmp = tempfile.mkstemp(dir=directory, prefix=".embedding_cache-",
                                       suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(snapshot, f)
                os.replace(tmp, self.cache_path)
            except BaseException:
                # Never leave the temp file behind on a failed write.
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
        except OSError:
            # An unwritable cache directory is not a reason to fail a summary.
            return

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
