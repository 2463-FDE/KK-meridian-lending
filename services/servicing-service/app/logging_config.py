"""Logging setup -- wires a stream handler and a file handler. Logs nothing itself.

Output goes to logs/payment-service.log: this service's legacy duplicate of
payment-service's charge path writes to the filename handed over in the repo.
Gitignored since PR #9; the file itself remains in git history (DEBT.md D18).
`payments.charge()` receives only processor_token/last4/brand (ADR 0008), so no
card data reaches this logger. Why this docstring used to say otherwise: D5c.
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
