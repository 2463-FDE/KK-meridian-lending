"""
PCI/PII redactor — strip sensitive fields before any log call.

Copied from services/loan-assistant/app/redactor.py (Week 1) — same
dependency-free module (stdlib only), same rules. Ported here to close D5:
payment-service's charge() logged full PAN/CVV/SSN at INFO with no redaction
at all.

Rules:
  - PAN (13-19 digit card number, any real card scheme, Luhn-validated): → [PAN-REDACTED]
  - CVV (3-4 digits mentioned near cvv/cvc/security code/card verification value,
    any phrasing) → [CVV-REDACTED]
  - SSN (ddd-dd-dddd, ddd dd dddd, or ddddddddd with no separator) → [SSN-REDACTED]
  - Dict keys named pan/cvv/ssn/card_number/card_no/processor_token: value
    replaced inline (a vaulted processor token is opaque but still sensitive)

Fail closed: an ambiguous 9-digit run, or a cvv/cvc/security-code mention near any
3-4 digit number, is treated as PII and masked, even if it might be something else.

Never log raw output of this module — redaction is best-effort on strings.
The canonical safe path is redact_dict() on structured data before any I/O.
"""

import re
import copy

# Candidate digit runs of 13-19 total digits, optionally separated by a single
# space or dash after any digit. Luhn-validated in _redact_pan() before masking.
_PAN_CANDIDATE_RE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_SSN_RE = re.compile(r"\b\d{3}[-\s]?\d{2}[-\s]?\d{4}\b")
_CVV_RE = re.compile(
    r"(?i)\b(?:cvv|cvc|card verification value|security code)\b\D{0,20}(\d{3,4})\b",
)

_SENSITIVE_KEYS = frozenset(
    {"pan", "cvv", "ssn", "card_number", "card_no", "social_security_number",
     "processor_token"}
)


def _luhn_valid(digits: str) -> bool:
    total = 0
    for i, ch in enumerate(reversed(digits)):
        d = int(ch)
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        total += d
    return total % 10 == 0


def _redact_pan(match: re.Match) -> str:
    digits = re.sub(r"\D", "", match.group(0))
    if 13 <= len(digits) <= 19 and _luhn_valid(digits):
        return "[PAN-REDACTED]"
    return match.group(0)


def redact_str(text: str) -> str:
    text = _PAN_CANDIDATE_RE.sub(_redact_pan, text)
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
