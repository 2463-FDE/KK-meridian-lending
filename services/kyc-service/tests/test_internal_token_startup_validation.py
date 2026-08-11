"""An unusable internal token must stop the service booting, not each request.

PR #18 review: `INTERNAL_SERVICE_TOKEN` defaulting to empty meant a deployment
mistake surfaced only per-request, as a 401 the caller logged as a warning. The
result was a silent, per-applicant identity-verification outage rather than a
loud deploy failure -- and silence is the expensive part, since nothing about the
running system looked wrong.

Two properties matter and are easy to get backwards:

  * **an unset ENVIRONMENT is treated as production.** A container boots without
    one, so "unset" is a real reachable deployment state. Defaulting it to dev
    would make the check pass in exactly the case it exists to catch.
  * **known-default values fail as hard as an empty one.** A secret published in
    this repository is not a secret: anyone who can read `docker-compose.yml`
    has it. Accepting it outside a dev box would make every downstream token
    check theatre.

The same validator is copied into origination-service and gateway (no shared
library in this repo), and the last test asserts that rather than trusting it.
"""
import pathlib

import pytest

from app import config


PROD_ENVS = ["", "production", "prod", "staging", "PRODUCTION"]
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("env", PROD_ENVS)
@pytest.mark.parametrize("token", sorted(config.KNOWN_DEV_TOKENS))
def test_known_or_empty_tokens_refuse_to_start_outside_dev(env, token):
    with pytest.raises(config.InsecureInternalTokenError) as excinfo:
        config.validate_internal_token(environment=env, token=token)

    message = str(excinfo.value)
    assert "INTERNAL_SERVICE_TOKEN" in message
    assert "ENVIRONMENT" in message, "the error must say what to set, not just that it failed"


@pytest.mark.parametrize("env", PROD_ENVS)
def test_a_real_secret_starts_normally(env):
    """Generated, not hand-written.

    This used to pass a 26-character hand-written string, which the length floor
    added in review round 2 now correctly refuses. The sample was modelling a
    secret no deployment should have had in the first place, so it moved to the
    generator the error message recommends rather than the floor being lowered
    to accommodate it.
    """
    import secrets as _secrets
    config.validate_internal_token(environment=env, token=_secrets.token_urlsafe(32))


@pytest.mark.parametrize("env", ["development", "dev", "test", "local", "TEST"])
@pytest.mark.parametrize("token", ["", "dev-internal-token-change-me", "test-internal-token"])
def test_dev_environments_may_use_the_published_defaults(env, token):
    """Local work and CI must stay possible; the check is about deployments."""
    config.validate_internal_token(environment=env, token=token)


def test_the_compose_default_is_one_of_the_known_values():
    """If someone changes the compose default, this list must change with it.

    Otherwise the new default silently becomes an accepted production secret --
    the exact hole this validator exists to close, reopened by a one-line edit
    somewhere else in the repository.
    """
    compose = (pathlib.Path(__file__).resolve().parents[3] / "docker-compose.yml").read_text(encoding="utf-8")
    assert "dev-internal-token-change-me" in compose, (
        "the compose default changed; add the new value to KNOWN_DEV_TOKENS in "
        "every service's config.py, or this validator will accept it in production"
    )
    assert "dev-internal-token-change-me" in config.KNOWN_DEV_TOKENS


@pytest.mark.parametrize("service", ["kyc-service", "origination-service", "gateway"])
def test_every_service_on_the_kyc_path_validates_at_startup(service):
    """Copied code, asserted rather than assumed.

    There is no shared library here, so the validator is duplicated three times.
    A copy that exists but is never called is indistinguishable from no check at
    all, so this checks both the definition and the call site.
    """
    root = pathlib.Path(__file__).resolve().parents[3] / "services" / service / "app"
    assert "def validate_internal_token" in (root / "config.py").read_text(encoding="utf-8"), service
    assert "config.validate_internal_token()" in (root / "main.py").read_text(encoding="utf-8"), (
        f"{service} defines the validator but never calls it at startup"
    )


# --- a denylist is not a policy (applied from PR #22's review) ----------------

@pytest.mark.parametrize("weak", ["1", "abc", "password", "hunter2", "a" * 31])
def test_a_short_or_guessable_token_refuses_to_boot(weak):
    """`INTERNAL_SERVICE_TOKEN=1` used to boot cleanly.

    A denylist can only reject the weak values someone thought of. Reviewed on
    PR #22 against servicing-service and applied here too -- the validator is
    duplicated across services because this repo has no shared library, and a
    policy that holds in one copy and not the others is not a policy.
    """
    with pytest.raises(config.InsecureInternalTokenError):
        config.validate_internal_token(environment="production", token=weak)


@pytest.mark.parametrize("padded", [
    "ChangeMe-padded-out-to-thirty-two-plus",
    "this-is-a-placeholder-value-really-long",
    "TODO-generate-a-real-secret-before-prod",
])
def test_a_long_placeholder_is_still_refused(padded):
    """Length is not entropy. A sentence is not a secret, however long."""
    assert len(padded) >= config.MIN_TOKEN_LENGTH
    with pytest.raises(config.InsecureInternalTokenError):
        config.validate_internal_token(environment="production", token=padded)


def test_a_generated_secret_is_accepted():
    """The policy must not reject what .env.example tells operators to use."""
    import secrets as _secrets
    config.validate_internal_token(environment="production", token=_secrets.token_urlsafe(32))


@pytest.mark.parametrize("service", ["kyc-service", "origination-service", "gateway"])
def test_the_policy_is_identical_in_every_copy(service):
    """Duplicated code, asserted rather than assumed.

    Three copies of this validator exist. A hardening applied to one of them and
    not the others leaves the weakest copy defining the real security posture,
    and nothing would report it.
    """
    cfg = (REPO_ROOT / "services" / service / "app" / "config.py").read_text(encoding="utf-8")
    assert "MIN_TOKEN_LENGTH = 32" in cfg, f"{service} has no length floor"
    assert "PLACEHOLDER_PATTERNS" in cfg, f"{service} does not reject placeholders"


def test_every_service_given_the_token_also_declares_its_environment():
    """A service that validates at startup must be told what environment it is in.

    This is the bug that reached CI on PR #22: a validating service received
    INTERNAL_SERVICE_TOKEN from compose but no ENVIRONMENT. Startup validation
    treats an unset ENVIRONMENT as production -- correctly, since a container
    boots without one -- so the service refused to start anywhere `.env` was
    absent, and `.env` is gitignored, meaning everywhere except a developer's own
    machine.

    Derived from the source rather than listed: any service whose config.py
    defines the validator must also receive ENVIRONMENT, so a service that gains
    validation later is covered the day it does.
    """
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    blocks, current, buf = {}, None, []
    for line in compose.splitlines():
        if line.startswith("  ") and not line.startswith("    ") and line.rstrip().endswith(":"):
            if current:
                blocks[current] = "\n".join(buf)
            current, buf = line.strip().rstrip(":"), []
        elif current:
            buf.append(line)
    if current:
        blocks[current] = "\n".join(buf)

    def _validates(service):
        cfg = REPO_ROOT / "services" / service / "app" / "config.py"
        return cfg.is_file() and "def validate_internal_token" in cfg.read_text(encoding="utf-8")

    missing = [
        name for name, body in blocks.items()
        if "INTERNAL_SERVICE_TOKEN" in body and "ENVIRONMENT" not in body and _validates(name)
    ]
    assert not missing, (
        f"these services validate at startup but receive no ENVIRONMENT: {missing}. "
        "Each refuses to boot wherever .env is absent -- which is every environment "
        "except a developer's own machine."
    )
