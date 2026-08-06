"""Logging setup -- handler wiring only (stream + file). Logs nothing itself.

Stale-docstring fix, same as origination-service's copy of this file. The old text
claimed "Logs the full request body on every POST -- including PII. No redaction.
(D5)". This service has no request-body middleware and no call site that logs a
payload; `decision.py` logs `app_id`, scores and reason codes, never applicant
PII, and `decision_events` deliberately stores no SSN or PAN either.

The wrong claim was copy-pasted across four services' logging_config.py, which is
how one inaccurate comment became four -- and why D5a read as an open finding long
after the behaviour was fixed.
"""
import logging
import os

LOG_DIR = os.getenv("LOG_DIR", "logs")


def get_logger(name: str) -> logging.Logger:
    logger = logging.getLogger(name)
    if logger.handlers:
        return logger
    logger.setLevel(os.getenv("LOG_LEVEL", "INFO"))
    fmt = logging.Formatter("%(levelname)s %(asctime)s %(name)s %(message)s")

    sh = logging.StreamHandler()
    sh.setFormatter(fmt)
    logger.addHandler(sh)

    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        fh = logging.FileHandler(os.path.join(LOG_DIR, "decision-service.log"))
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass
    return logger
