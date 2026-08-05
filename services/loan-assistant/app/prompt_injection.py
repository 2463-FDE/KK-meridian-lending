"""Prompt-injection heuristic guard for user-supplied questions before they
reach retrieval or the LLM.

Not a full injection-detection model -- a curated pattern list catching the
common override/jailbreak phrasings ("ignore previous instructions", "you are
now", "reveal your system prompt", etc.). Loan officers can type arbitrary
free text into policy-chat; this is the first thing that text hits, before
retrieve()/classify_answerable() or any LLM call. A question this flags never
reaches the LLM at all -- same fail-closed posture as classify_answerable()
refusing to hand an ungrounded chunk to the model.

False positives are expected and accepted: a legitimate policy question is
extremely unlikely to contain these specific phrasings, so erring toward
blocking is the right tradeoff here, not a full NLP classifier.
"""
import re

_INJECTION_PATTERNS = [
    re.compile(r"(?i)\bignore\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above)\s+instructions?\b"),
    re.compile(r"(?i)\bdisregard\s+(all\s+|any\s+)?(the\s+)?(previous|prior|above)\b"),
    re.compile(r"(?i)\byou\s+are\s+now\b"),
    re.compile(r"(?i)\bnew\s+instructions?\s*:"),
    re.compile(r"(?i)\b(reveal|show|print|output)\s+(your\s+|the\s+)?(system\s+)?prompt\b"),
    re.compile(r"(?i)\bact\s+as\s+(if\s+you\s+(are|were)|an?)\b"),
    re.compile(r"(?i)\bpretend\s+(you|to\s+be)\b"),
    re.compile(r"(?i)\bjailbreak\b"),
    re.compile(r"(?i)\bDAN\b[^.]{0,20}\bmode\b"),
    re.compile(r"(?i)\bdeveloper\s+mode\b"),
]


def contains_injection_attempt(text: str) -> bool:
    return any(p.search(text) for p in _INJECTION_PATTERNS)
