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
import re
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
- If the excerpt says the code, system or runtime does NOT yet implement the
  policy it states, your answer MUST say so as well. State the policy, then state
  plainly that Meridian's system does not currently apply it. Never present the
  policy as describing what the system does today. This is not optional and it is
  not a caveat to compress away: an answer that states the rule without it tells a
  reader the borrower is charged that way, which would be false.
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


#: How much implementation-status text may ride along with the policy excerpt.
#:
#: `fee_schedule.md`'s section is several chunks; this keeps the useful head of
#: it -- the mismatch, and what the code does instead -- without pushing the
#: prompt toward `llm_client.MAX_INPUT_TOKENS`, which `answer_policy_question`
#: still checks afterwards either way.
_STATUS_CONTEXT_CHARS = 1400

#: Text in a policy chunk that points at a runtime-versus-policy section.
#:
#: `policies/fee_schedule.md` writes the pointer as "The code does not yet
#: implement this -- see 'Current implementation differs' below." Matching the
#: CLAIM rather than the cross-reference wording, so re-phrasing the pointer does
#: not silently switch the behaviour off.
_CAVEAT_POINTER = re.compile(
    r"(?:code|system|runtime)\s+does\s+not\s+(?:yet\s+)?implement"
    r"|current implementation differs"
    r"|not (?:yet )?implemented",
    re.IGNORECASE,
)


def _context_for(top_hit: dict, hits: list[dict], chunks: list[dict]) -> str:
    """The grounding excerpt, plus the implementation-status section it refers to.

    THE DEFECT THIS EXISTS FOR. `fee_schedule.md` publishes the client's decided
    late-fee rule and, in a section of its own, records that the code does not
    implement it and what it charges instead. Chunking splits those into
    `fee_schedule.md#2.0` and `#3.0`. Only `hits[0]` was ever put in the prompt, so
    the model received a policy row ending "see 'Current implementation differs'
    below" and no such section -- a pointer to text it did not have. Asked "What is
    the late fee?" it answered with the decided rule and no caveat, which reads as
    a statement about what Meridian charges today. It is not: the runtime still
    applies the superseded arrears rule (`docs/DEBT.md` D23).

    So when the grounding excerpt CLAIMS its policy is unimplemented, the section
    that says what the code actually does is appended to the same context. Both
    halves are retrieved corpus text -- nothing is synthesised, and the answer stays
    as grounded as it was before.

    DELIBERATELY NARROW. This adds nothing when the top hit carries no such claim,
    so an eligibility or APR answer is untouched. It is not a disclaimer bolted
    onto every response; it is the evidence the corpus already contains, reaching
    the model that is supposed to read it.
    """
    text = top_hit["text"]
    if not _CAVEAT_POINTER.search(text):
        return text

    doc_id = top_hit.get("doc_id")
    # The status section of the SAME document, in DOCUMENT order.
    #
    # Order matters and ranking does not. This is a section written to be read top
    # to bottom: it opens by saying the policy and the code differ, and only then
    # names what the code actually charges. Taking the best-ranked fragment took
    # the opening paragraph alone, which supports "not implemented" but not "and
    # this is what it does instead" -- so an answer could say the rule is
    # unimplemented without being able to say what a borrower is actually charged.
    #
    # Bounded by characters rather than by chunk count, so a re-chunk of the
    # policy file cannot quietly change how much is sent.
    section = [c["text"] for c in chunks
               if c.get("implementation_status") and c.get("doc_id") == doc_id]
    if not section:
        return text

    budget = _STATUS_CONTEXT_CHARS
    kept: list[str] = []
    for piece in section:
        if piece in text:
            continue
        if len(piece) > budget:
            break
        kept.append(piece)
        budget -= len(piece)
    if not kept:
        return text
    return text + "\n\n" + "\n\n".join(kept)


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
    context_text = _context_for(top_hit, hits, chunks)
    prompt = _build_prompt(safe_question, context_text)

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
