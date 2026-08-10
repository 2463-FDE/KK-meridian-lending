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
     "processor_token",
     # Cardholder name. Reviewed finding (D5d): charge() logged it in clear at
     # INFO alongside payment context, because this set did not treat it as
     # sensitive. A name beside a loan id, an amount and a last4 identifies a
     # person and what they paid -- which is the thing log redaction exists to
     # prevent, whether or not it is card data under PCI.
     #
     # The call site no longer passes it at all; this entry is the backstop, so
     # that a future one cannot reintroduce the leak merely by including the
     # field. Both spellings, since either is plausible for the same value.
     "name", "cardholder_name"}
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


def looks_sensitive(text: str) -> bool:
    """True if `text` carries one of the shapes this module knows how to redact.

    For deciding whether to REJECT a value rather than mask it. Redaction covers
    the log; it does nothing about a value that gets stored, so a caller-supplied
    field that lands on a database row needs to be refused at the boundary
    instead (`schemas.PaymentIn.idempotency_key`, reviewed on PR #16).

    Defined here, next to the patterns, on purpose: a second private copy of
    "looks like a PAN" in the schema module would drift from this one, and this
    is the copy with tests and Luhn validation behind it. Equivalent to "does
    redaction change this string", which is the property actually wanted -- so it
    cannot silently disagree with `redact_str`.
    """
    return redact_str(text) != text


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
