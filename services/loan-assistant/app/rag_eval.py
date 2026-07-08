"""RAG retrieval eval harness over the policy corpus.

Week 2 (real curriculum brief) deliverable. Proves retrieval quality against a fixed
query set, and the report explicitly names two gaps instead of just returning search
results:
  (a) queries about a specific denial reason correctly find nothing, because no
      reason was ever recorded anywhere (RF-18) -- not a retrieval bug
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

# Fixed eval query set. Positive cases check retrieval actually surfaces the right
# fact (expected_keyword must appear in the top chunk). The two gap cases check
# ground truth on the WHOLE corpus (must_not_contain absent from every chunk, not
# just the top-k) -- this proves the corpus structurally cannot answer them, rather
# than depending on a similarity-score threshold that a small corpus's word overlap
# can trip either way (e.g. "denied" and "application" both appear in the general
# Reg B policy section without it containing any applicant-specific fact).
EVAL_QUERIES = [
    {"query": "what is the minimum age to apply for a loan", "expected_keyword": "18"},
    {"query": "what happens if my credit score is in the refer band", "expected_keyword": "counteroffer"},
    {"query": "what is the late fee amount", "expected_keyword": "$35"},
    {"query": "why was application 6012 denied", "must_not_contain": "6012"},
    {"query": "what beneficial owner documentation do we require for an LLC", "must_not_contain": "beneficial owner"},
]

TOP_K = 3


def retrieve(query: str, chunks: list[dict], embedder: LocalTfidfEmbedder, idf: dict, k: int = TOP_K) -> list[dict]:
    q_vec = apply_idf(embedder.embed(query), idf)
    scored = []
    for chunk in chunks:
        c_vec = apply_idf(embedder.embed(chunk["text"]), idf)
        scored.append({**chunk, "score": cosine_similarity(q_vec, c_vec)})
    scored.sort(key=lambda c: c["score"], reverse=True)
    return scored[:k]


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
    corpus_text = "\n".join(c["text"] for c in chunks).lower()

    query_results = []
    for case in EVAL_QUERIES:
        hits = retrieve(case["query"], chunks, embedder, idf)
        top_text = hits[0]["text"] if hits else ""

        if "expected_keyword" in case:
            correct = case["expected_keyword"].lower() in top_text.lower()
            check = f"expects '{case['expected_keyword']}' in top chunk"
        else:
            correct = case["must_not_contain"].lower() not in corpus_text
            check = f"expects '{case['must_not_contain']}' absent from entire corpus"

        query_results.append(
            {
                "query": case["query"],
                "check": check,
                "top_score": round(hits[0]["score"], 4) if hits else 0.0,
                "top_chunk": hits[0]["chunk_id"] if hits else None,
                "correct": correct,
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
                "Queries about a specific denial reason correctly return no relevant "
                "hit -- not a retrieval failure. No reason field exists anywhere in "
                "decisions or kb_dump for any application (RF-18). Retrieval cannot "
                "surface data that was never recorded."
            ),
            "pii_in_corpus_source": pii_finding,
        },
    }


if __name__ == "__main__":
    import pprint

    pprint.pprint(run_eval())
