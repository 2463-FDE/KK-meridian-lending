"""Logging setup -- wires a stream handler and a file handler. Logs nothing itself.

This module used to describe `payments.charge()`'s log line and the shape bounds
on `main.py`'s `PaymentIn` that kept a card number out of it. Both are gone:
servicing's processorless `POST /payments` was retired with D2, so this service
no longer has a charge path or that schema.

What remains true, and is why the rule still matters here: no servicing log call
site takes a caller-supplied string. The money routes log ids and amounts, and
`routers/loans.py::_display_last4` reads `last4` only -- never a card number,
asserted by `tests/test_pan_mask.py::test_the_display_never_reads_a_pan_attribute`.
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
