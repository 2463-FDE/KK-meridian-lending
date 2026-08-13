"""Test baseline environment.

`config.INTERNAL_SERVICE_TOKEN` is read once at import time, and an unset value
can never match a request header (see routers/kyc.py) -- which is the fail-closed
behaviour we want in a deploy and useless in a test suite that needs to reach the
handler at all. Set it here, at module level rather than in a fixture, so it is in
place before `app.config` is first imported.

Same shape as decision-service/tests/conftest.py.
"""
import os

os.environ.setdefault("INTERNAL_SERVICE_TOKEN", "test-internal-token")

INTERNAL_TOKEN = os.environ["INTERNAL_SERVICE_TOKEN"]

#: Every authorized POST /kyc/check in this suite sends these headers. Imported
#: rather than repeated so a future change to the header name is one edit.
AUTH_HEADERS = {"X-Internal-Token": INTERNAL_TOKEN}
