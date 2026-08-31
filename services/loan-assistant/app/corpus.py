"""Corpus loading + chunking for the policy RAG index.

Only `policies/*.md` is eligible for the corpus. `kb_dump/applications.jsonl` must
never be embedded raw -- it carries unredacted SSN/PAN. See adr/0005 for the corpus
hygiene decision and rag_eval.check_kb_dump_pii() for the offline check that proves it.

Every chunk is redaction-checked before being added -- if the redactor ever flags a
policy doc chunk, that's a real bug (policy docs should be clean) and this refuses to
silently embed it.
"""
import os
import re

from .redactor import redact_str

_HEADER_RE = re.compile(r"^#{1,6}\s+.+$")

# Default assumes a local checkout (services/loan-assistant/app -> repo root ->
# policies/). Override with POLICIES_DIR in any environment where that relative
# layout doesn't hold -- confirmed live: it doesn't inside this service's own
# Docker image (WORKDIR /app, COPY app ./app means the same "..","..",".." only
# walks up to filesystem root, not the repo root), so docker-compose.yml sets
# POLICIES_DIR explicitly alongside the ./policies volume mount.
POLICIES_DIR = os.getenv(
    "POLICIES_DIR",
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "policies"),
)


class CorpusHygieneError(Exception):
    """Raised when a document intended for the corpus contains PII the redactor caught."""


#: A section that documents how the RUNTIME differs from the policy above it.
#:
#: `policies/fee_schedule.md` publishes the client's decided late-fee rule and
#: then, in a section of its own, states that the code does not implement it and
#: what it does instead. That section exists because the file is served to Policy
#: Chat -- it says so in its own words -- and an answer quoting a rule the runtime
#: does not apply would be wrong in front of a client.
#:
#: Chunking split the two apart. The policy row ends "see 'Current implementation
#: differs' below", and that section became a SEPARATE chunk, so an answer built
#: from the policy row alone was handed a pointer to text it did not have. This
#: marks such a section so the answer path can keep the two together.
#:
#: Recognised by HEADING rather than by scanning prose for phrases like "not
#: implemented": a heading is a deliberate authoring act by whoever maintains the
#: policy file, where a phrase match would fire on any paragraph that happened to
#: discuss implementation. `policies/README` documents the convention.
_IMPLEMENTATION_STATUS_HEADING = re.compile(
    r"^#{1,6}\s*Current implementation differs\b", re.IGNORECASE | re.MULTILINE)


def _is_implementation_status(paragraph: str) -> bool:
    """True when this paragraph is a runtime-versus-policy section."""
    return bool(_IMPLEMENTATION_STATUS_HEADING.search(paragraph))


#: A chunk that CLAIMS its own policy is unimplemented and points at the section
#: saying what happens instead. Distinct from `_is_implementation_status`, which
#: recognises the section itself by its heading.
#:
#: Defined here, once, because two answer paths need it -- `policy_chat` for the
#: chat prompt and `policy_tool` for the agent's excerpts. Two copies of this
#: expression would be two definitions of "does this policy admit it is not
#: implemented", and the day they disagreed one surface would go back to stating
#: an unimplemented rule as current practice.
_CAVEAT_POINTER = re.compile(
    r"(?:code|system|runtime)\s+does\s+not\s+(?:yet\s+)?implement"
    r"|current implementation differs"
    r"|not (?:yet )?implemented",
    re.IGNORECASE,
)


def points_to_implementation_status(text: str) -> bool:
    return bool(_CAVEAT_POINTER.search(text))


def _chunk(text: str, doc_id: str, max_chars: int = 500) -> list[dict]:
    """Split on blank-line paragraph boundaries, then hard-wrap long paragraphs.

    A lone markdown header paragraph gets merged into the paragraph that follows
    it rather than emitted as its own chunk. Confirmed live: a blank line between
    a "## Section" heading and its content (standard markdown, both policy docs
    do this everywhere) made the header its own content-free chunk -- asking
    about a section by its heading name ("Eligibility", "Credit decisioning")
    retrieved that bare heading as the top hit instead of the actual bullet list
    right after it, since the content paragraph never repeats the heading word.

    Review finding on the first version of this fix: it tracked a single
    `pending_header` scalar, so two consecutive headers (e.g. an "## H2"
    immediately followed by an "### H3" before any body text) silently dropped
    the first one -- it was overwritten, never appended anywhere, with no error.
    Fixed by accumulating a list of pending headers instead of one scalar, so
    any run of consecutive headers merges together with whatever content
    eventually follows (or with each other, if a header sits at end of file).
    """
    raw_paragraphs = [p.strip() for p in text.split("\n\n") if p.strip()]
    paragraphs: list[str] = []
    pending_headers: list[str] = []
    for para in raw_paragraphs:
        if _HEADER_RE.match(para) and "\n" not in para:
            pending_headers.append(para)
            continue
        if pending_headers:
            para = "\n".join(pending_headers + [para])
            pending_headers = []
        paragraphs.append(para)
    if pending_headers:
        # Trailing header(s) with nothing after them (end of file) -- keep
        # them rather than silently dropping them.
        paragraphs.append("\n".join(pending_headers))

    # A status SECTION is its heading paragraph and everything under it until the
    # next heading of the same or higher level. Marking only the heading paragraph
    # was not enough: `fee_schedule.md`'s section opens by saying policy and code
    # differ and then, in the NEXT paragraph, names what the code actually charges
    # (`min($35, 5% of balances.past_due)`). An answer that gets the first without
    # the second can say "not implemented" but not what IS implemented.
    status_flags: list[bool] = []
    in_status = False
    status_level = 0
    for para in paragraphs:
        first_line = para.split(chr(10), 1)[0]
        heading = _HEADER_RE.match(first_line)
        if _is_implementation_status(para):
            in_status = True
            status_level = len(first_line) - len(first_line.lstrip("#"))
        elif heading and in_status:
            level = len(first_line) - len(first_line.lstrip("#"))
            if level <= status_level:
                in_status = False
        status_flags.append(in_status)

    chunks = []
    for i, para in enumerate(paragraphs):
        status = status_flags[i]
        for j in range(0, len(para), max_chars):
            piece = para[j : j + max_chars]
            chunks.append(
                {
                    "doc_id": doc_id,
                    "chunk_id": f"{doc_id}#{i}.{j // max_chars}",
                    "text": piece,
                    # Does this chunk describe what the CODE does, as distinct
                    # from what the policy says? See `_is_implementation_status`.
                    "implementation_status": status,
                }
            )
    return chunks


def load_policy_corpus(policies_dir: str = POLICIES_DIR) -> list[dict]:
    """Load + chunk the policy docs. Raises CorpusHygieneError if any chunk fails
    the redaction check -- policy docs are expected to be clean, so a failure here
    means investigate before ingesting, not silently redact and continue."""
    chunks = []
    for fname in sorted(os.listdir(policies_dir)):
        if not fname.endswith(".md"):
            continue
        path = os.path.join(policies_dir, fname)
        with open(path, encoding="utf-8") as f:
            text = f.read()
        for chunk in _chunk(text, doc_id=fname):
            safe = redact_str(chunk["text"])
            if safe != chunk["text"]:
                raise CorpusHygieneError(
                    f"Redactor caught PII in {chunk['chunk_id']} -- refusing to add "
                    "it to the corpus. Policy docs should be clean; investigate "
                    "before ingesting."
                )
            chunks.append(chunk)
    return chunks
