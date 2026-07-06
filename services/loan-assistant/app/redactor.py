"""
PCI/PII redactor — strip sensitive fields before any log call or LLM prompt.

Rules:
  - PAN (16-digit card number): any format → [PAN-REDACTED]
  - CVV (3–4 digits in context): → [CVV-REDACTED]
  - SSN (ddd-dd-dddd): → [SSN-REDACTED]
  - Dict keys named pan/cvv/ssn/card_number/card_no: value replaced inline

Never log raw output of this module — redaction is best-effort on strings.
The canonical safe path is redact_dict() on structured data before any I/O.
"""

import re
import copy

_PAN_RE = re.compile(r"\b(?:\d[ -]?){15}\d\b")
_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CVV_RE = re.compile(
    r'(?i)(?:"cvv"\s*:\s*"|cvv=|cvv:\s*)["\s]*(\d{3,4})',
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
