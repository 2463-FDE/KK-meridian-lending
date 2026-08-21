"""The embedding cache must survive the agent calling its tool in parallel.

Found by running the containerised app, not by reading code. The summary
returned `502 AgentProviderError (JSONDecodeError)` after two successful policy
tool calls, and the JSON that failed to decode was the embedding cache.

`_save_cache` opened the real path with `"w"`, which truncates immediately and
serialises afterwards. A reader arriving inside that window saw an empty file.
Latent for as long as retrieval ran once, server-side, in sequence -- and live
the moment the agent shipped, because a model turn can emit several tool calls
at once and LangGraph runs them in parallel threads, each building an embedder
over the same file.

Three tests, deliberately of different kinds. The concurrency one reproduces the
failure; a race test alone can pass on a broken build, so the other two assert
the properties that make the race impossible, deterministically.
"""
import concurrent.futures
import json
import pathlib

import pytest

from app import policy_tool
from app.embeddings import LocalTfidfEmbedder


@pytest.fixture
def cache_path(tmp_path):
    return str(tmp_path / ".embedding_cache.json")


# --------------------------------------------------------------------------
# Deterministic: the properties that make the race impossible.
# --------------------------------------------------------------------------

def test_a_failed_write_leaves_the_previous_cache_intact(cache_path, monkeypatch):
    """Atomicity, asserted by breaking the write halfway.

    With truncate-then-write, a write that dies mid-serialise leaves a
    half-file on the real path. With write-then-replace, the real path still
    holds the last good version.
    """
    first = LocalTfidfEmbedder(cache_path=cache_path)
    first.embed("late payment fee")
    good = pathlib.Path(cache_path).read_text(encoding="utf-8")
    assert json.loads(good), "precondition: a real cache was written"

    import app.embeddings as embeddings

    def _explode(*args, **kwargs):
        raise RuntimeError("disk full")

    monkeypatch.setattr(embeddings.json, "dump", _explode)

    second = LocalTfidfEmbedder(cache_path=cache_path)
    with pytest.raises(RuntimeError):
        second.embed("a different string entirely")

    assert pathlib.Path(cache_path).read_text(encoding="utf-8") == good, (
        "a failed write damaged the cache that was already on disk")


def test_a_failed_write_leaves_no_temporary_file_behind(cache_path, monkeypatch):
    """Otherwise a full disk turns into a directory full of debris."""
    import app.embeddings as embeddings

    LocalTfidfEmbedder(cache_path=cache_path).embed("late payment fee")
    monkeypatch.setattr(embeddings.json, "dump",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("disk full")))

    with pytest.raises(RuntimeError):
        LocalTfidfEmbedder(cache_path=cache_path).embed("something else")

    leftovers = list(pathlib.Path(cache_path).parent.glob(".embedding_cache-*.tmp"))
    assert not leftovers, f"temporary files left behind: {leftovers}"


@pytest.mark.parametrize("garbage", ["", "   ", "{not json", '{"a": ', "\x00\x01"])
def test_an_unreadable_cache_is_treated_as_absent(cache_path, garbage):
    """A cache is an optimisation. Every entry is recomputable, so an unreadable
    one must cost time and nothing else -- it must not fail a summary, which is
    precisely what it did in the container."""
    pathlib.Path(cache_path).write_text(garbage, encoding="utf-8")

    embedder = LocalTfidfEmbedder(cache_path=cache_path)
    vector = embedder.embed("late payment fee")

    assert vector, "the embedder produced nothing from a recoverable state"
    assert json.loads(pathlib.Path(cache_path).read_text(encoding="utf-8")), (
        "the cache was not rewritten cleanly")


def test_an_unwritable_cache_directory_does_not_fail_the_caller(tmp_path):
    """A read-only mount is a deployment condition, not a summary failure."""
    unwritable = tmp_path / "no-such-directory" / ".embedding_cache.json"

    vector = LocalTfidfEmbedder(cache_path=str(unwritable)).embed("late payment fee")

    assert vector


# --------------------------------------------------------------------------
# The reproduction.
# --------------------------------------------------------------------------

def test_parallel_embedders_do_not_corrupt_the_cache(cache_path):
    """Eight threads over one cache file, which reproduced the container failure.

    Iteration count is high enough that the pre-fix code failed every run
    observed; a lower one made the test decorative.
    """
    texts = [f"late payment fee schedule underwriting policy variant {i}"
             for i in range(60)]
    errors = []

    def _worker(offset):
        try:
            embedder = LocalTfidfEmbedder(cache_path=cache_path)
            for text in texts[offset::8]:
                embedder.embed(text)
        except Exception as exc:  # noqa: BLE001 - the failure mode is the subject
            errors.append(f"{type(exc).__name__}: {exc}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(_worker, range(8)))

    assert not errors, f"concurrent embedders failed: {sorted(set(errors))}"
    json.loads(pathlib.Path(cache_path).read_text(encoding="utf-8"))


def test_the_policy_tool_survives_being_called_in_parallel(monkeypatch, tmp_path):
    """The shape the agent actually produces.

    A model turn emitting several tool calls is not hypothetical -- the real
    Bedrock run emitted three at once, and LangGraph executes them in parallel
    threads.
    """
    import app.embeddings as embeddings

    monkeypatch.setattr(embeddings, "_DEFAULT_CACHE_PATH",
                        str(tmp_path / ".embedding_cache.json"))
    policy_tool.reset_cache()
    failures = []

    def _call(i):
        try:
            policy_tool.search_underwriting_policy(f"late fee policy {i}")
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{type(exc).__name__}: {exc}")

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(_call, range(12)))
    policy_tool.reset_cache()

    assert not failures, f"parallel policy tool calls failed: {sorted(set(failures))}"
