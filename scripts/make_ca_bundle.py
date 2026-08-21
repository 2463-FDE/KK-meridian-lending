"""Build a CA bundle that works inside the Linux containers, on this machine.

Why this exists: a TLS-inspecting proxy (here, Norton Web/Mail Shield) presents
a certificate chain of length 1 -- the leaf, with no issuer -- so anything
verifying against public `certifi` fails with "unable to get local issuer
certificate". On Windows the interception CA is already in the OS trust store,
so the host works. A Linux container has no such store, which is why the same
credentials succeed on the host and fail in the service.

What this writes: `certs/<name>.pem` = certifi's bundle plus the interception
roots found in the Windows trust store. `certs/` is gitignored, so the CA is
never committed -- it is generated per machine and mounted read-only by
docker-compose, which already declares `./certs:/app/certs:ro`.

What it deliberately does NOT do:
  * disable or weaken verification anywhere;
  * commit a corporate root CA to the repository;
  * make verification optional at runtime.

Usage:
    python scripts/make_ca_bundle.py            # auto-detect interception roots
    python scripts/make_ca_bundle.py --match Zscaler

Then set, in .env.loan-assistant.local (NOT the shared .env -- other services
do not mount ./certs and would fail with FileNotFoundError):

    SSL_CERT_FILE=/app/certs/proxy-ca-bundle.pem
    REQUESTS_CA_BUNDLE=/app/certs/proxy-ca-bundle.pem

Both are needed: httpx reads SSL_CERT_FILE, langsmith's requests-based client
reads REQUESTS_CA_BUNDLE and ignores the other.
"""
import argparse
import pathlib
import ssl
import sys

REPO = pathlib.Path(__file__).resolve().parents[1]
DEFAULT_OUT = REPO / "certs" / "proxy-ca-bundle.pem"

#: Vendors whose products commonly intercept TLS on a corporate laptop. Used
#: only to pick certificates OUT of the machine's own trust store -- nothing is
#: downloaded, and nothing outside that store is ever trusted.
DEFAULT_MATCHES = ("Norton", "Zscaler", "Netskope", "Blue Coat", "Forcepoint",
                   "Sophos", "Fortinet", "Kaspersky", "ESET", "Bitdefender")


def interception_roots(matches):
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives.serialization import Encoding
    except ImportError:
        sys.exit("pip install cryptography first")

    if not hasattr(ssl, "enum_certificates"):
        sys.exit("this script reads the Windows trust store; on Linux/macOS "
                 "export the root CA from your own trust store instead")

    found = []
    for store in ("ROOT", "CA"):
        for der, _enc, _trust in ssl.enum_certificates(store):
            try:
                cert = x509.load_der_x509_certificate(der)
            except Exception:
                continue
            subject = cert.subject.rfc4514_string()
            if any(m.lower() in subject.lower() for m in matches):
                found.append((subject, cert.public_bytes(Encoding.PEM)))
    return found


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--match", action="append", default=None,
                        help="substring of the CA subject (repeatable)")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()

    import certifi

    matches = args.match or list(DEFAULT_MATCHES)
    roots = interception_roots(matches)
    if not roots:
        sys.exit(f"no interception CA found in the trust store matching {matches}. "
                 f"If your proxy uses a different name, pass --match.")

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(pathlib.Path(certifi.where()).read_bytes() + b"\n"
                    + b"".join(pem for _s, pem in roots))

    print(f"wrote {out} ({out.stat().st_size} bytes)")
    for subject, _pem in roots:
        print(f"  + {subject[:80]}")
    print("\nSet in .env.loan-assistant.local:")
    print(f"  SSL_CERT_FILE=/app/certs/{out.name}")
    print(f"  REQUESTS_CA_BUNDLE=/app/certs/{out.name}")


if __name__ == "__main__":
    main()
