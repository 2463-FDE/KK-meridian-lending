"""
PCI/PII redactor — strip sensitive fields before any log call or LLM prompt.

Rules:
  - PAN (16-digit card number): any format → [PAN-REDACTED]
  - CVV (3-4 digits mentioned near "cvv"/"security code", any phrasing) → [CVV-REDACTED]
  - SSN (ddd-dd-dddd, ddd dd dddd, or ddddddddd with no separator) → [SSN-REDACTED]
  - Dict keys named pan/cvv/ssn/card_number/card_no: value replaced inline

Fail closed: an ambiguous 9-digit run or a "cvv"/"security code" mention near any
3-4 digit number is treated as PII and masked, even if it might be something else.
Leaking real PII to a third-party LLM is worse than over-redacting a false positive.

Never log raw output of this module — redaction is best-effort on strings.
The canonical safe path is redact_dict() on structured data before any I/O.
"""

import re
import copy

_PAN_RE = re.compile(r"\b(?:\d[ -]?){15}\d\b")
_SSN_RE = re.compile(r"\b\d{3}[-\s]?\d{2}[-\s]?\d{4}\b")
_CVV_RE = re.compile(
    r"(?i)\b(?:cvv|security code)\b\D{0,10}(\d{3,4})\b",
)

_SENSITIVE_KEYS = frozenset(
    {"pan", "cvv", "ssn", "card_number", "card_no", "social_security_number"}
)


def redact_str(text: str) -> str:
    text = _PAN_RE.sub("[PAN-REDACTED]", text)
    text = _SSN_RE.sub("[SSN-REDACTED]", text)
    text = _CVV_RE.sub(lambda m: m.group(0).replace(m.group(1), "[CVV-REDACTED]"), text)
    return text


def redact_dict(data: dict) -> dict:
    """Return a deep copy with sensitive key values replaced."""
    result = copy.deepcopy(data)
    _redact_node(result)
    return result


def _redact_node(node):
    if isinstance(node, dict):
        for key, value in node.items():
            if key.lower() in _SENSITIVE_KEYS:
                node[key] = "[REDACTED]"
            elif isinstance(value, str):
                node[key] = redact_str(value)
            else:
                _redact_node(value)
    elif isinstance(node, list):
        for i, item in enumerate(node):
            if isinstance(item, str):
                node[i] = redact_str(item)
            else:
                _redact_node(item)
