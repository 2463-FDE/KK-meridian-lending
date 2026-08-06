"""Logging setup -- handler wiring only (stream + file). Logs nothing itself.

Output goes to logs/kyc-service.log (gitignored; docker-compose mounts ./logs).

Stale-docstring fix, same as the origination-service and decision-service copies.
The old text claimed "Logs the full request body on every POST -- including PII.
No redaction. (D5)". There is no request-body middleware in this service, and
`run_cip` logs the applicant id and the four CIP booleans, not the identity fields
it checked.

Note what is genuinely open here, so this correction is not misread as closing it:
D11 -- KYC is CIP-only, with no OFAC/sanctions screening, no UBO capture and no
ongoing monitoring. That is a scope limit, not a logging defect.
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
