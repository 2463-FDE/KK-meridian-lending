"""Logging setup -- wires a stream handler and a file handler. Logs nothing itself.

Output goes to logs/kyc-service.log (gitignored; docker-compose mounts ./logs).
`run_cip` logs the four CIP booleans, and the router logs `application_id`/
`applicant_id` -- not the identity fields being checked.
Why this docstring used to say otherwise: DEBT.md D5c.
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
        fh = logging.FileHandler(os.path.join(LOG_DIR, "kyc-service.log"))
        fh.setFormatter(fmt)
        logger.addHandler(fh)
    except OSError:
        pass
    return logger
