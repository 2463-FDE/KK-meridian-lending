"""SEC-11's triage has to keep matching the tree it triaged.

A dependency triage is a claim with a shelf life. It says which advisories are
fixed, which are unreachable and why, and every one of those statements is
falsifiable by an ordinary edit somebody makes for an unrelated reason. The row
this guards had already gone stale once: it quoted counts measured on
2026-08-26 as though they were current, and by 2026-09-01 they were wrong in
both directions.

WHAT THIS PINS, and it is deliberately not "the counts are correct" -- those move
whenever an advisory is published, and a test that failed on somebody else's
disclosure schedule would be deleted within a month. What it pins is the part
that is this repository's own responsibility:

  1. Both upgrades the triage says it applied are actually pinned -- and for
     `next`, pinned in the lockfile as well, because `npm ci` installs the lock
     and a package.json edit alone would leave the audited version unchanged.
  2. The "not reachable" arguments still hold. Each rests on an API this estate
     does not call, and each is checkable: the day somebody adds a `FileResponse`
     or an X.509 verification, the accepted finding stops being accepted and this
     says so BEFORE the row is read as though it still applied.
  3. The row still says the audits are non-blocking for a stated reason, while
     CI still has them non-blocking -- so the two cannot drift apart.
  4. CI runs the exact frontend audit command the row quotes its counts from.

A WARNING THIS FILE EARNED. Point 2 is only worth having when the acceptance
really does rest on the argument. An earlier version of this guard asserted
three Next.js surfaces -- no middleware, no `"use server"`, no `next/image` --
and was cited as proof that `next@15.1.3`'s two criticals were unreachable. One
of them, GHSA-9qr9-h5gf-34mp, needs none of those three; its surface is App
Router RSC handling, which this frontend is. The guard passed, and its passing
was read as evidence for a claim it had never tested. A green check on the wrong
surface is more dangerous than no check, because it ends the conversation. The
Next acceptance is now withdrawn and replaced by a version pin, which is a claim
this file can actually verify.
"""
import json
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]

#: The version SEC-11 records as closing GHSA-9qr9-h5gf-34mp. Named once so the
#: row, package.json and the lockfile are all compared against the same literal.
NEXT_PIN = "15.5.25"

#: The frontend audit command, exactly as CI must run it. SEC-11 quotes its
#: counts from `--omit=dev`; the job used to omit that flag. Pinned as a literal
#: because the two commands agree TODAY -- comparing their output would not have
#: caught the drift, and will not catch it next time either.
FRONTEND_AUDIT_CMD = "npm audit --omit=dev --audit-level=high"
DEBT = REPO / "docs" / "DEBT.md"
CI = REPO / ".github" / "workflows" / "ci.yml"
GATEWAY_REQS = REPO / "services" / "gateway" / "requirements.txt"
SERVICING_REQS = REPO / "services" / "servicing-service" / "requirements.txt"

#: Application source only. Tests may legitimately mention an API in order to
#: assert it is absent, and a fixture is not a call from a served route.
_APP_DIRS = sorted((REPO / "services").glob("*/app"))


def _sec11_row() -> str:
    for line in DEBT.read_text(encoding="utf-8").splitlines():
        if line.startswith("| **SEC-11**"):
            return " ".join(line.split())
    raise AssertionError("no SEC-11 row in docs/DEBT.md")


def _app_sources():
    for app_dir in _APP_DIRS:
        for path in app_dir.rglob("*.py"):
            yield path


def _uses(needle: str) -> list:
    """Application files that mention `needle`, comments and docstrings included.

    Deliberately generous: for a reachability argument, a false positive costs a
    conversation and a false negative costs the argument. If a file so much as
    names `FileResponse`, this guard would rather ask than assume.
    """
    hits = []
    for path in _app_sources():
        if needle in path.read_text(encoding="utf-8"):
            hits.append(path.relative_to(REPO).as_posix())
    return hits


# ---------------------------------------------------------------------------
# 1. The upgrade the row says it made.
# ---------------------------------------------------------------------------

def test_the_cryptography_pin_is_the_one_the_triage_claims():
    """`48.0.1`, in both services that verify the Ed25519 principal.

    GHSA-537c-gmf6-5ccf is in the OpenSSL statically linked into 48.0.0's
    wheels. This is the dependency on the money path, so a silent revert to
    48.0.0 -- easy to do while resolving a conflict -- must not leave the
    register claiming it was fixed.
    """
    for path in (GATEWAY_REQS, SERVICING_REQS):
        text = path.read_text(encoding="utf-8")
        assert "cryptography==48.0.1" in text, (
            f"{path.relative_to(REPO).as_posix()} no longer pins "
            "cryptography==48.0.1, which SEC-11 records as the one upgrade the "
            "triage applied")
        assert "cryptography==48.0.0" not in text


def test_the_row_names_the_version_it_pinned():
    assert "48.0.1" in _sec11_row(), (
        "SEC-11 no longer names the version it upgraded to, so a reader cannot "
        "check the claim against requirements.txt")


# ---------------------------------------------------------------------------
# 2. The reachability arguments.
# ---------------------------------------------------------------------------

def test_the_cryptography_acceptances_still_rest_on_unused_primitives():
    """Three advisories are accepted because PKCS7 and X.509 are never used.

    If either appears, those acceptances are void -- and the failure has to
    arrive when the API is introduced, not when somebody next reads the row.
    """
    for api, advisories in (("pkcs7", "PYSEC-2026-3552"),
                            ("x509", "PYSEC-2026-3553 / 3554")):
        hits = _uses(api)
        assert hits == [], (
            f"{api} is now used ({hits}), so SEC-11's acceptance of "
            f"{advisories} no longer holds. Re-triage before this row is read "
            "as current.")


def test_the_starlette_acceptances_still_rest_on_unused_apis():
    """Five advisories are accepted because these APIs are never called."""
    for api in ("FileResponse", "StaticFiles", "HTTPEndpoint", "UploadFile",
                ".form("):
        hits = _uses(api)
        assert hits == [], (
            f"{api} is now used ({hits}), so at least one starlette advisory "
            "SEC-11 accepted as unreachable may now be reachable")


def test_the_open_starlette_question_is_still_the_one_the_row_describes():
    """`request.url` in the rate limiter is the UNKNOWN the row names.

    The row says seven uses are error logging and the eighth is the rate
    limiter's exempt-path check. If that changes -- more uses, or a use that is
    not `.path` -- the description stops matching and the "unknown, narrow"
    framing has to be redone rather than inherited.
    """
    uses = []
    for path in _app_sources():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "request.url" in line and not line.strip().startswith("#"):
                uses.append((path.relative_to(REPO).as_posix(), line.strip()))

    assert uses, "request.url is no longer used at all -- SEC-11 says it is"
    non_path = [u for u in uses if "request.url.path" not in u[1]]
    assert non_path == [], (
        "request.url is now used for more than its .path, so the starlette "
        "URL-reconstruction advisories reach further than SEC-11 describes: %s"
        % non_path)

    limiter = [u for u in uses if "rate_limit.py" in u[0]]
    assert limiter, (
        "the rate limiter no longer reads request.url.path -- SEC-11 names that "
        "as the one place the reconstruction question could touch a control")


def test_the_next_pin_is_the_one_the_triage_claims():
    """`next@15.5.25`, in package.json AND in the lockfile.

    THIS TEST REPLACED A WRONG ONE, and the replacement is the point.

    The previous version asserted that `frontend/` has no `middleware.*`, no
    `"use server"` action and no `next/image` use, and treated that as proof
    that `next@15.1.3`'s two criticals were unreachable. It was the wrong
    surface. GHSA-f82v-jwr5-mffw (authorization bypass in middleware) does need
    middleware, so for that one the argument held. GHSA-9qr9-h5gf-34mp (RCE in
    the React flight protocol) needs none of the three -- its surface is App
    Router RSC handling, which `frontend/app/layout.tsx` is. So the guard passed
    green over a reachable critical, which is worse than no guard: it was
    evidence for a claim it had never tested.

    The version is the honest thing to pin. A reachability argument is only
    worth pinning when the acceptance rests on it, and this acceptance is
    withdrawn -- the advisory is fixed by the upgrade, not dodged by the
    configuration. Both files are checked because a `package.json` edit without
    a matching lockfile is how a dependency upgrade silently does not happen:
    `npm ci` installs from the lock.
    """
    pkg = json.loads((REPO / "frontend" / "package.json").read_text(encoding="utf-8"))
    declared = pkg["dependencies"]["next"]
    assert declared == NEXT_PIN, (
        "frontend/package.json declares next==%s, but SEC-11 records %s as the "
        "upgrade that closed GHSA-9qr9-h5gf-34mp. Re-triage the row before this "
        "pin moves." % (declared, NEXT_PIN))

    lock = json.loads((REPO / "frontend" / "package-lock.json").read_text(encoding="utf-8"))
    locked = lock["packages"]["node_modules/next"]["version"]
    assert locked == NEXT_PIN, (
        "package-lock.json resolves next to %s while package.json declares %s. "
        "`npm ci` installs the lock, so the audited version is the locked one."
        % (locked, NEXT_PIN))
    assert lock["packages"][""]["dependencies"]["next"] == NEXT_PIN


def test_the_row_names_the_next_version_it_pinned():
    row = _sec11_row()
    assert NEXT_PIN in row, (
        "SEC-11 no longer names the next version it upgraded to, so a reader "
        "cannot check the claim against frontend/package.json")
    assert "15.1.3" in row, (
        "SEC-11 no longer names the version it upgraded FROM. The misclassified "
        "acceptance is the history this row exists to keep -- deleting it makes "
        "the correction invisible.")


def test_the_frontend_remainder_is_still_build_time_only():
    """`postcss` and `nanoid` are accepted because no input reaches them.

    Both need attacker-controlled CSS -- an unescaped `</style>` reaching the
    stringifier, or a `sourceMappingURL` comment naming a file to read. The only
    stylesheet postcss sees is authored in this repository, and the served image
    runs `node server.js` with no CSS pipeline in it.

    Each condition below is one clause of that argument. A config file, a
    runtime import or a second stylesheet source would each break it
    independently, which is why they are checked separately rather than as one
    assertion nobody could interpret when it failed.
    """
    frontend = REPO / "frontend"

    configs = list(frontend.glob("postcss.config.*")) + \
        list(frontend.glob("tailwind.config.*"))
    assert configs == [], (
        "a postcss/tailwind config now exists (%s), so postcss is doing more "
        "than compiling one repository-authored stylesheet and SEC-11's "
        "build-time-only argument needs redoing"
        % [c.name for c in configs])

    importers = []
    for sub in ("app", "components", "lib"):
        base = frontend / sub
        if not base.exists():
            continue
        for path in list(base.rglob("*.ts")) + list(base.rglob("*.tsx")):
            text = path.read_text(encoding="utf-8")
            if "postcss" in text or "nanoid" in text:
                importers.append(path.relative_to(REPO).as_posix())
    assert importers == [], (
        "postcss or nanoid is now referenced from application code (%s), so "
        "they are no longer build-time only" % importers)

    config = (frontend / "next.config.mjs").read_text(encoding="utf-8")
    assert 'output: "standalone"' in config, (
        "next.config.mjs no longer builds a standalone server, which is the "
        "half of SEC-11's postcss argument that says no CSS pipeline ships")


# ---------------------------------------------------------------------------
# 3. The blocking decision, and CI agreeing with it.
# ---------------------------------------------------------------------------

def test_the_row_and_ci_agree_that_the_audits_are_non_blocking():
    """Two halves of one decision, which must not drift apart.

    The row explains WHY the audits are not blocking -- accepted findings with
    no allowlist for a job to consult -- and CI is where that decision is
    actually expressed. If somebody flips `continue-on-error` without the
    allowlist, the build goes permanently red on findings this row already
    dispositioned, and the row will still be explaining a decision nobody made.
    """
    ci = CI.read_text(encoding="utf-8")
    audit_steps = re.findall(r"- name: (?:pip-audit|npm audit)[^\n]*\n(\s+)continue-on-error: true",
                             ci)
    row = _sec11_row()

    if "NOT YET" in row.upper():
        assert len(audit_steps) == 2, (
            "SEC-11 says the audits stay non-blocking, but CI no longer marks "
            "both audit steps continue-on-error. Update the row with the "
            "allowlist that made blocking safe.")
    else:
        assert audit_steps == [], (
            "SEC-11 no longer says the audits are non-blocking while CI still "
            "makes them so")


def test_ci_runs_the_frontend_audit_command_the_row_quotes():
    """One command, named in two places, and they must be the same one.

    SEC-11 attributes its frontend counts to `npm audit --omit=dev`. CI ran
    `npm audit --audit-level=high` with no `--omit=dev`, so the row reasoned
    about what ships while the job measured what ships plus Playwright, eslint,
    typescript and the type packages.

    WHY THIS IS PINNED AS A STRING rather than by comparing outputs: the two
    commands currently report the same three packages. Any test that diffed
    their results would have passed throughout the drift. The defect was never
    a wrong number -- it was a claim about provenance that nothing checked.
    """
    ci = CI.read_text(encoding="utf-8")
    assert FRONTEND_AUDIT_CMD in ci, (
        "CI no longer runs %r. SEC-11 quotes its frontend counts from that "
        "command; if the job is changed, change the row in the same commit."
        % FRONTEND_AUDIT_CMD)

    stray = re.findall(r"^\s*npm audit(?! --omit=dev --audit-level=high)[^\n]*$",
                       ci, re.MULTILINE)
    assert stray == [], (
        "a second npm audit invocation exists in CI (%s), so which one SEC-11's "
        "counts came from is ambiguous again" % stray)

    row = _sec11_row()
    assert "--omit=dev" in row, (
        "SEC-11 no longer names the audit scope its frontend counts came from")


def test_the_row_states_what_would_make_blocking_safe():
    """A deferral without a condition is a deferral nobody can close."""
    row = _sec11_row().lower()
    assert "allowlist" in row, (
        "SEC-11 defers blocking CI audits without naming what has to exist "
        "first, which makes the deferral unfalsifiable")
