import os

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://meridian:postgres@postgres:5432/meridian",
)
# processor key from environment only — no hardcoded fallback. Set via secrets manager.
PROCESSOR_API_KEY = os.getenv("PROCESSOR_API_KEY", "")
PROCESSOR_BASE_URL = os.getenv("PROCESSOR_BASE_URL", "https://api.cardprocessor.example.com")

# Review fix: charge() used to treat a processor_token as proof of a real
# charge -- it was never actually sent to a processor, only shape-checked.
# Same fail-closed convention as decision-service's ALLOW_CREDIT_STUB/
# ALLOW_MODEL_STUB: an unset ENVIRONMENT is a real, reachable production
# misconfiguration, not a test convenience gap -- outside dev/test, a
# missing PROCESSOR_API_KEY must refuse to authorize a charge rather than
# silently approve one against a fake processor. See app/processor.py.
ENVIRONMENT = os.getenv("ENVIRONMENT", "").lower()
ALLOW_PAYMENT_STUB = ENVIRONMENT in ("development", "dev", "test", "local")
# servicing-service base URL — we call it to apply a captured payment to the balance
SERVICING_URL = os.getenv("SERVICING_URL", "http://servicing-service:8002")
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")

# Required on every POST /payments (see routers/payments.py) -- defense in
# depth for the network boundary (payment-service has no host port mapping;
# see docker-compose.yml). Unset means no caller can ever match it, so a
# deploy that forgets to configure this fails closed rather than open.
INTERNAL_SERVICE_TOKEN = os.getenv("INTERNAL_SERVICE_TOKEN", "")
