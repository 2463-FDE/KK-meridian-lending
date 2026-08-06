"""Logging setup -- handler wiring only (stream + file). Logs nothing itself.

Output goes to logs/payment-service.log -- this service's legacy duplicate of
payment-service's charge path writes to the same filename that was handed over in
the repo. The file is gitignored now (PR #9); it was tracked, and remains in git
history (D18).

Stale-docstring fix. The old text claimed this module "writes the full charge
request body (PAN, CVV, SSN) at INFO. No redaction. (D5, #7)". Two things make
that wrong now: this module only wires handlers, and the legacy
`payments.charge()` it serves no longer receives PAN, CVV or SSN at all -- it
takes an opaque `processor_token` plus `last4`/`brand` for display, so there is
nothing left to redact (ADR 0008; the servicing half of D5a closed with the Week 5
tokenization work).
"""
import logging
import os

LOG_DIR = os.getenv("LOG_DIR", "logs")


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))
    fmt = logging.Formatter("%(levelname)s %(asctime)s %(message)s")

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        fh = logging.FileHandler(os.path.join(LOG_DIR, "payment-service.log"))
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass
    return logger
