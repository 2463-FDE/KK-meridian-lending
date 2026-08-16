"""Generate the gateway's principal-signing keypair.

The gateway signs a short-lived assertion naming the human behind a
money-moving request; servicing verifies it with the public half and therefore
cannot forge one (spec 0002 REQ-ID-3). That asymmetry is the whole control, so
the two halves must end up in different places:

    PRINCIPAL_SIGNING_KEY   gateway ONLY -- the private half
    PRINCIPAL_VERIFY_KEY    servicing (and any future verifier) -- public

Usage:

    python db/tools/generate_principal_keypair.py            # print both
    python db/tools/generate_principal_keypair.py --env      # .env-ready lines

Nothing is written to disk and no key is committed. A key that lives in this
repository is not a key -- the same rule `INTERNAL_SERVICE_TOKEN` follows, and
for a sharper reason here: anyone holding the private half can mint an admin.

Ed25519 rather than RSA: one algorithm, no parameter choices to get wrong, and
small enough to paste into an environment variable without a file mount.
"""
import argparse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def generate() -> tuple[str, str]:
    private = Ed25519PrivateKey.generate()
    private_pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("utf-8")
    public_pem = private.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode("utf-8")
    return private_pem, public_pem


def one_line(pem: str) -> str:
    """A PEM as a single assignable value.

    `.env` and `$GITHUB_ENV` are line-based; a PEM is not. The newlines are
    escaped here and decoded by each service on the way in
    (`config._pem_from_env`). That decode was missing when this tool was first
    written: the keys were written escaped, read raw, and every money route
    refused with a 401 that looked like an authorization failure rather than a
    malformed key.
    """
    return pem.strip().replace("\n", "\\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env", action="store_true",
        help="print as single-line .env assignments (newlines escaped)",
    )
    parser.add_argument(
        "--github-env", action="store_true",
        help="print unquoted KEY=value lines for appending to $GITHUB_ENV",
    )
    args = parser.parse_args()

    private_pem, public_pem = generate()
    if args.github_env:
        # Unquoted: GitHub Actions takes $GITHUB_ENV lines literally and would
        # carry quote characters into the value, producing a PEM that fails to
        # parse at boot. CI generates a pair per run for the same reason a
        # developer does -- there is nothing to commit and nothing to rotate.
        print(f"PRINCIPAL_SIGNING_KEY={one_line(private_pem)}")
        print(f"PRINCIPAL_VERIFY_KEY={one_line(public_pem)}")
        return
    if args.env:
        print(f'PRINCIPAL_SIGNING_KEY="{one_line(private_pem)}"')
        print(f'PRINCIPAL_VERIFY_KEY="{one_line(public_pem)}"')
        return

    print("# --- gateway only: PRINCIPAL_SIGNING_KEY -------------------------")
    print(private_pem.rstrip())
    print()
    print("# --- servicing (and any verifier): PRINCIPAL_VERIFY_KEY ----------")
    print(public_pem.rstrip())


if __name__ == "__main__":
    main()
