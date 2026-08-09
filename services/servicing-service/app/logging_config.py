"""Logging setup -- wires a stream handler and a file handler. Logs nothing itself.

`payments.charge()` logs `loan_id`, `amount` and `method`, and nothing else.

A CARD NUMBER cannot reach this logger through any of the three, because each is
shape-bounded in main.py's PaymentIn rather than merely named: `method` is
`Literal["card","ach"]`, `loan_id` is the int4 range `loans.id` can actually
hold, and `amount` is a positive figure with a ceiling far above any consumer
instalment payment. That is the reason to state -- `extra="forbid"` rejects
unknown FIELD NAMES and says nothing about what a permitted field contains, so
this claim was false twice before it was true: first through `method`, which
took any string, then through `loan_id`, which took any integer and is logged
BEFORE the insert that would reject a nonexistent loan. Reviewed on PR #16 both
times.

WHAT THIS DOES NOT CLAIM: a nine-digit `loan_id` is indistinguishable from an
SSN, and no bound can separate them -- 412559981 is a plausible id. The
guarantee here is card-number-shaped data, not all identity data. `last4` and
`brand` are shape-checked too; they are persisted rather than logged.

Output goes to logs/payment-service.log: this service's legacy duplicate of
payment-service's charge path writes to the filename handed over in the repo.
Gitignored since PR #9; the file itself remains in git history (DEBT.md D18).
Why this docstring used to claim a charge-request-body log: D5c.
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
