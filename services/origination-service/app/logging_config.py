"""Logging setup -- handler wiring only (stream + file). Logs nothing itself.

Stale-docstring fix. This module used to open with "Logs the full request body on
every POST -- including PII. No redaction. Halcyon said 'we need the body to
debug.' (D5)". That describes no code here or anywhere in this service: there is
no request-body middleware, and `intake.create_application` stopped logging the
payload (PR #6 review, Gap C). The intake log line now carries `app_id` and
`applicant_id` only -- see tests/test_intake_pii_not_logged.py, which fails if a
PII canary reaches the log.

Kept as a note rather than deleted, because the stale claim had a cost: D5a was
reported as a live PII-logging gap twice -- once in docs/DEBT.md, once in a later
documentation pass -- by readers who trusted this comment instead of the code. A
comment that overstates a defect produces false findings as reliably as one that
understates it.
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
        fh = logging.FileHandler(os.path.join(LOG_DIR, "origination-service.log"))
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass
    return logger
