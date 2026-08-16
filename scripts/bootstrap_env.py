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


def _one_line(pem: str) -> str:
    """A PEM as a single .env value.

    `.env` is line-based and a PEM is not, so the newlines are escaped. Both
    services decode the escape before parsing, and a key that fails to parse is a
    boot failure rather than a per-request surprise.
    """
    return pem.strip().replace("\n", "\\n")


def _generate_principal_keypair() -> tuple[str, str]:
    """An Ed25519 pair: private for the gateway, public for servicing.

    Imported lazily so this script still runs on a checkout whose service
    requirements have not been installed -- bootstrap is the first thing a new
    developer runs, and failing on an import would send them to fix the wrong
    problem.
    """
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    private = Ed25519PrivateKey.generate()
    return (
        private.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode(),
        private.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        ).decode(),
    )


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

    # The principal signing pair, for the same reason and on the same terms as
    # the token: compose requires both halves, nothing in the repository is a
    # usable key, and whoever holds the private half can mint an admin.
    #
    # Generated together so they always match. A mismatched pair is the worst of
    # the failure modes -- everything boots, and every money route refuses with a
    # 401 that looks like an authorization problem rather than a key problem.
    #
    # PEM is multi-line and .env is not, so newlines are escaped; both services
    # decode them before parsing.
    private_pem, public_pem = _generate_principal_keypair()
    text, signing_added = _set_if_missing(
        text, "PRINCIPAL_SIGNING_KEY", _one_line(private_pem))
    text, verify_added = _set_if_missing(
        text, "PRINCIPAL_VERIFY_KEY", _one_line(public_pem))
    ENV.write_text(text, encoding="utf-8")

    if signing_added or verify_added:
        print("generated the principal signing pair (local only -- never commit it)")
    if token_added:
        print("generated INTERNAL_SERVICE_TOKEN (local only -- never commit it)")
    if env_added:
        print("set ENVIRONMENT=development")
    if not (token_added or env_added):
        print(".env already complete; nothing changed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
