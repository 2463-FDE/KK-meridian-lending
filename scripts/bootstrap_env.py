"""Create a usable local `.env` for a clean checkout.

`docker-compose.yml` supplies **no** default for `INTERNAL_SERVICE_TOKEN` or
`ENVIRONMENT`, on purpose:

* a token committed to this repository is not a secret -- the failure it defends
  against is a port re-exposed or the network boundary bypassed, which is exactly
  the situation where an attacker can read the repo;
* `validate_internal_token()` skips every strength and known-token check when
  `ENVIRONMENT` is development/dev/test/local, so a base file defaulting to
  `development` would hand any production-like deploy money-moving routes guarded
  by whatever string was typed, including a short or public one.

Requiring both is therefore correct, and it also broke the documented quick
start: `make up` on a clean checkout hit an interpolation error. This is the
documented way a developer supplies them, and it is the same path CI exercises,
so the quick start cannot rot without the build noticing.

Writes only to `.env`, which is gitignored. The generated token never leaves the
machine that ran this and must never be committed.

Idempotent: an existing non-empty value is left exactly as it is, so re-running
never rotates a token out from under a running stack.
"""
import pathlib
import re
import secrets
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
ENV = REPO / ".env"
EXAMPLE = REPO / ".env.example"


def _set_if_missing(text: str, key: str, value: str) -> tuple[str, bool]:
    """Set `key` only when it is absent or empty. Returns (text, changed)."""
    if re.search(rf"^{re.escape(key)}=.+$", text, re.M):
        return text, False
    if re.search(rf"^{re.escape(key)}=.*$", text, re.M):
        return re.sub(rf"^{re.escape(key)}=.*$", f"{key}={value}", text, count=1, flags=re.M), True
    return text.rstrip("\n") + f"\n{key}={value}\n", True


def main() -> int:
    if not ENV.exists():
        if not EXAMPLE.exists():
            print("no .env.example to copy from", file=sys.stderr)
            return 1
        ENV.write_text(EXAMPLE.read_text(encoding="utf-8"), encoding="utf-8")
        print("created .env from .env.example")

    text = ENV.read_text(encoding="utf-8")
    text, token_added = _set_if_missing(text, "INTERNAL_SERVICE_TOKEN", secrets.token_urlsafe(32))
    text, env_added = _set_if_missing(text, "ENVIRONMENT", "development")
    ENV.write_text(text, encoding="utf-8")

    if token_added:
        print("generated INTERNAL_SERVICE_TOKEN (local only -- never commit it)")
    if env_added:
        print("set ENVIRONMENT=development")
    if not (token_added or env_added):
        print(".env already complete; nothing changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
