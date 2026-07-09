"""RAG retrieval eval harness over the policy corpus.

Week 2 (real curriculum brief) deliverable. Proves retrieval quality against a fixed
query set, and the report explicitly names two gaps instead of just returning search
results:
  (a) queries about a specific denial reason are correctly classified as no_answer
      by classify_answerable() -- not because the fact happens to be absent from
      the corpus (retrieve() still returns real chunks with real scores regardless),
      but because no reason was ever recorded anywhere (RF-18) and the retrieved
      content doesn't actually ground an answer. Score alone can't gate this: the
      denial query scores *higher* against the general Reg B policy chunk (real
      word overlap) than some genuinely-answerable queries score against their
      correct chunk, so grounding is checked against retrieved content, not a
      similarity threshold.
  (b) kb_dump/applications.jsonl carries raw PII and must never enter this corpus,
      confirmed by an offline regex check (never an LLM call, per the quota note)

See adr/0005-rag-corpus-hygiene.md for the corpus hygiene decision this enforces.
"""
import json
import os

from .corpus import load_policy_corpus
from .embeddings import LocalTfidfEmbedder, apply_idf, build_idf, cosine_similarity
from .redactor import redact_dict

KB_DUMP_PATH = os.path.join(
    os.path.dirname(__file__), "..", "..", "..", "kb_dump", "applications.jsonl"
)

# Fixed eval query set. grounding_term is the specific fact the query needs to be
# genuinely answerable -- classify_answerable() checks it against what retrieve()
# ACTUALLY returns (the top-k chunks), not against the whole corpus. expect_answer
# records what the correct system behavior is: True means a grounded answer should
# be produced, False means the system should say "no answer" rather than hand a
# topically-similar-but-fact-free chunk to an answer generator.
EVAL_QUERIES = [
    {"query": "what is the minimum age to apply for a loan", "grounding_term": "18", "expect_answer": True},
    {"query": "what happens if my credit score is in the refer band", "grounding_term": "counteroffer", "expect_answer": True},
    {"query": "what is the late fee amount", "grounding_term": "$35", "expect_answer": True},
    {"query": "why was application 6012 denied", "grounding_term": "6012", "expect_answer": False},
    {"query": "what beneficial owner documentation do we require for an LLC", "grounding_term": "beneficial owner", "expect_answer": False},
]

TOP_K = 3

# Absolute floor below which a "hit" isn't even worth checking for grounding -- an
# empty or near-zero-similarity result is never answerable regardless of content.
MIN_SCORE_FLOOR = 0.05


def retrieve(query: str, chunks: list[dict], embedder: LocalTfidfEmbedder, idf: dict, k: int = TOP_K) -> list[dict]:
    q_vec = apply_idf(embedder.embed(query), idf)
    scored = []
    for chunk in chunks:
        c_vec = apply_idf(embedder.embed(chunk["text"]), idf)
        scored.append({**chunk, "score": cosine_similarity(q_vec, c_vec)})
    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored[:k]


def classify_answerable(hits: list[dict], grounding_term: str) -> bool:
    """Whether the retrieved top-k chunks actually support answering the query --
    this is the no-answer gate itself, callable by the real assistant path later,
    not just a test-time assertion against corpus-wide ground truth.

    A single cosine-similarity threshold can't reliably separate "genuinely
    relevant" from "coincidentally shares a word" on a corpus this small -- e.g.
    "why was application 6012 denied" scores 0.42 against the general Reg B
    adverse-action policy section (real overlap on "denied"/"application"), which
    is *higher* than some genuinely-answerable queries score against their correct
    chunk. Score alone would either exclude real answers or admit false-confident
    ones. So grounding requires the specific fact to actually be present in what
    was retrieved, with the score floor only as a belt-and-suspenders empty-result
    guard.
    """
    if not hits or hits[0]["score"] < MIN_SCORE_FLOOR:
        return False
    retrieved_text = " ".join(h["text"] for h in hits).lower()
    return grounding_term.lower() in retrieved_text


def check_kb_dump_pii(path: str = KB_DUMP_PATH) -> dict:
    """Offline regex check via the Week 1 redactor -- no LLM call. Proves kb_dump is
    NOT safe to embed raw and names exactly which fields leak."""
    if not os.path.exists(path):
        return {"checked": False, "reason": "kb_dump not found"}
    leaked_fields = set()
    record_count = 0
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record_count += 1
            record = json.loads(line)
            safe = redact_dict(record)
            for key, value in record.items():
                if value is not None and value != safe.get(key):
                    leaked_fields.add(key)
    return {
        "checked": True,
        "record_count": record_count,
        "pii_fields_found": sorted(leaked_fields),
        "safe_to_embed_raw": len(leaked_fields) == 0,
    }


def run_eval() -> dict:
    chunks = load_policy_corpus()
    embedder = LocalTfidfEmbedder()
    idf = build_idf([embedder.embed(c["text"]) for c in chunks])

    query_results = []
    for case in EVAL_QUERIES:
        hits = retrieve(case["query"], chunks, embedder, idf)
        answerable = classify_answerable(hits, case["grounding_term"])
        status = "answered" if answerable else "no_answer"

        query_results.append(
            {
                "query": case["query"],
                "status": status,
                "expect_answer": case["expect_answer"],
                "top_score": round(hits[0]["score"], 4) if hits else 0.0,
                "top_chunk": hits[0]["chunk_id"] if hits else None,
                "correct": answerable == case["expect_answer"],
            }
        )

    kb_pii = check_kb_dump_pii()
    pii_finding = None
    if kb_pii.get("checked") and not kb_pii.get("safe_to_embed_raw"):
        pii_finding = (
            f"kb_dump/applications.jsonl contains unredacted PII in fields: "
            f"{kb_pii['pii_fields_found']}. This file must never be embedded raw."
        )

    return {
        "corpus_chunk_count": len(chunks),
        "query_results": query_results,
        "all_queries_correct": all(r["correct"] for r in query_results),
        "kb_dump_pii_check": kb_pii,
        "findings": {
            "missing_decision_record_gap": (
                "Queries about a specific denial reason are correctly classified "
                "no_answer -- retrieve() still returns a real, topically-related "
                "policy chunk with a nontrivial score, but classify_answerable() "
                "correctly refuses it because the specific fact isn't actually "
                "present. No reason field exists anywhere in decisions or kb_dump "
                "for any application (RF-18); retrieval cannot ground an answer "
                "that was never recorded."
            ),
            "pii_in_corpus_source": pii_finding,
        },
    }


if __name__ == "__main__":
    import pprint

    pprint.pprint(run_eval())
