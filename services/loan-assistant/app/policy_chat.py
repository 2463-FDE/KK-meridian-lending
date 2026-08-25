"""Policy Q&A chat -- the feature Week 2's RAG work deliberately deferred.

rag_eval.py was scoped as an eval harness proving retrieval quality and
refusing to hallucinate (adr/0005), not a user-facing answer generator. This
module is that answer generator, built on top of the exact same retrieve() /
classify_answerable() gate rather than replacing it: a question that gate
marks unanswerable never reaches the LLM at all, so the guarantee ADR 0005
established (never hand a topically-similar-but-fact-free chunk to an answer
generator) holds here too, not just in the eval report.

Reuses llm_client's guardrails (provider-agnostic client construction, retry/
timeout wiring, markdown-fence stripping) rather than duplicating them -- this
prompt is a different shape (grounding excerpt + question, not application
data), so it gets its own system prompt, passed into the now-generalized
llm_client.call_api(client, prompt, system=...).
"""
import json
import logging

from pydantic import BaseModel, Field, ValidationError

from . import llm_client
from .corpus import load_policy_corpus
from .embeddings import LocalTfidfEmbedder, build_idf
from .prompt_injection import contains_injection_attempt
from .rag_eval import classify_answerable, retrieve
from .redactor import redact_str
from .schemas import PolicyAnswer

log = logging.getLogger(__name__)

_NOT_RECORDED_ANSWER = (
    "I don't have a recorded answer for that in the lending policy documents. "
    "This may need a human to confirm."
)

_INJECTION_BLOCKED_ANSWER = (
    "This question can't be processed as written -- it looks like it's trying "
    "to override the assistant's instructions rather than ask about lending "
    "policy. Rephrase it as a plain policy question."
)

_SYSTEM = """
You are a Meridian Lending policy assistant. You are given ONE excerpt from the
official lending policy documents and a question from a loan officer.

Rules:
- Answer using ONLY the provided excerpt. Do not use outside knowledge.
- If the excerpt does not actually contain the answer, say so plainly instead
  of guessing -- respond with answerable=false in that case.
- Keep the answer to 1-3 sentences.
- Never include SSN, full card numbers, or CVV in your output (none should be
  present in the excerpt, but never echo one if it somehow were).

Respond with only a JSON object: {"answerable": true|false, "answer": "..."}
No markdown fences, no extra text.
""".strip()

# Lazy, process-wide cache -- the corpus is static and rebuilding TF-IDF/IDF
# per request would re-read and re-chunk the policy files on every question
# for no benefit. LocalTfidfEmbedder already disk-caches individual chunk
# vectors; this just avoids redoing the corpus load + IDF pass every call.
_cache: dict = {}


def _corpus_state():
    if not _cache:
        chunks = load_policy_corpus()
        embedder = LocalTfidfEmbedder()
        idf = build_idf([embedder.embed(c["text"]) for c in chunks])
        _cache["chunks"] = chunks
        _cache["embedder"] = embedder
        _cache["idf"] = idf
    return _cache["chunks"], _cache["embedder"], _cache["idf"]


class PolicyChatResponseError(Exception):
    pass


class _ModelJsonResponse(BaseModel):
    """Strict schema for the model's raw JSON reply -- review finding:
    `bool(data.get("answerable", True))` trusted sloppy model output two ways:
    Python's bare `bool("false")` is True (any non-empty string is truthy), and
    a missing key silently defaulted to answerable. Routing through this model
    instead means a non-bool answerable value must be an actual parseable
    boolean (Pydantic's lax bool coercion correctly reads "false"/"true" as
    real booleans, unlike the builtin), the field must be present at all, and
    answer must be a real, non-empty string -- anything else is a validation
    error, not a silent guess."""

    answerable: bool
    answer: str = Field(min_length=1)


#: Bounded, closed set. A length is the most an operator can be told about a
#: question without being told the question, and a BUCKET rather than the exact
#: count because an exact length is a fingerprint: it distinguishes one question
#: from another and can confirm a guess about which one was asked.
_LENGTH_BUCKETS = ("tiny", "short", "medium", "long")


def _length_bucket(question: str) -> str:
    """Which bucket this question's length falls in. Never the length itself."""
    size = len(question or "")
    if size <= 40:
        return "tiny"
    if size <= 200:
        return "short"
    if size <= 1000:
        return "medium"
    return "long"


def _build_prompt(question: str, context_text: str) -> str:
    return (
        f"Policy excerpt:\n{context_text}\n\n"
        f"Question: {question}\n\n"
        "Respond with only the JSON object described in your system instructions."
    )


def answer_policy_question(question: str) -> PolicyAnswer:
    """Answer a loan-policy question, grounded strictly in the retrieved
    policy excerpt, or say plainly that it isn't recorded. Never calls the LLM
    for a question classify_answerable() has already flagged as ungrounded."""
    safe_question = redact_str(question)

    # Categorical only. This used to be `log.info("policy_chat question=%s",
    # safe_question)`, and redaction did not make that acceptable: the user's
    # query is on the client's prohibited-retention list as a category, and a
    # redacted user query is still a retained user query. The redactor removes
    # patterns it recognises -- an SSN, a card number -- and a policy question is
    # free text, so there is nothing for it to recognise and the question was
    # written to the log essentially intact.
    #
    # This file already applied the correct rule one function down, where the
    # parse-error branch says "a redacted model response is still a retained
    # model response. Stage and error class only." Same rule, applied to the
    # input as well as the output.
    #
    # What is left is a stage, an outcome, and a length bucket -- enough to see
    # traffic, refusal rates and whether questions are getting longer, without
    # retaining what anyone asked.
    log.info("policy_chat stage=policy_chat_request length_bucket=%s",
             _length_bucket(question))

    if contains_injection_attempt(safe_question):
        log.warning("policy_chat stage=policy_chat_request status=blocked "
                    "reason=suspected_injection")
        return PolicyAnswer(answerable=False, answer=_INJECTION_BLOCKED_ANSWER, source_chunk_id=None)

    chunks, embedder, idf = _corpus_state()
    hits = retrieve(safe_question, chunks, embedder, idf)

    if not classify_answerable(safe_question, hits):
        log.info("policy_chat stage=policy_chat_request status=unanswerable")
        return PolicyAnswer(answerable=False, answer=_NOT_RECORDED_ANSWER, source_chunk_id=None)

    top_hit = hits[0]
    prompt = _build_prompt(safe_question, top_hit["text"])

    # Same cost guard summarize_application() runs -- an arbitrary-length question
    # (up to PolicyChatIn's 4000-char schema cap) was reaching the LLM unchecked,
    # letting a large-but-schema-valid question trigger an oversized paid call.
    estimated = llm_client._estimate_tokens(_SYSTEM + prompt)
    if estimated > llm_client.MAX_INPUT_TOKENS:
        raise llm_client.LLMCostGuardError(
            f"Estimated input tokens ({estimated}) exceeds guard "
            f"({llm_client.MAX_INPUT_TOKENS}). Ask a shorter question."
        )

    client = llm_client.make_client()
    raw = llm_client.call_api(client, prompt, system=_SYSTEM)

    try:
        raw_data = json.loads(llm_client.strip_markdown_fences(raw))
        parsed = _ModelJsonResponse(**raw_data)
    except (json.JSONDecodeError, ValidationError, TypeError) as exc:
        # Same rule as llm_client's summary parse: a redacted model response is
        # still a retained model response. Stage and error class only.
        log.error("policy_chat parse error stage=answer_parse error=%s", type(exc).__name__)
        raise PolicyChatResponseError(
            f"Could not parse policy-chat response: {type(exc).__name__}") from exc

    is_answerable = parsed.answerable
    # The outcome, categorically. `answered` and `not_recorded` are both
    # "accepted" as far as this line is concerned -- the request was served --
    # and the model's own verdict is the thing worth counting separately.
    log.info("policy_chat stage=policy_chat_request status=accepted answerable=%s",
             "yes" if is_answerable else "no")
    return PolicyAnswer(
        answerable=is_answerable,
        answer=parsed.answer,
        source_chunk_id=top_hit["chunk_id"] if is_answerable else None,
        # The real retrieved excerpt, not just its id -- lets a reader verify
        # the answer against the actual policy text instead of trusting it on
        # faith (same "prove it" principle as rag_eval's own findings).
        source_text=top_hit["text"] if is_answerable else None,
    )
