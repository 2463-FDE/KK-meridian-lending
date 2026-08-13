"""A repository-known token must stop the service booting.

PR #22 review, high severity: the money-route guard was undercut by its own
configuration. `docker-compose.yml` started this service with
`dev-internal-token-change-me` whenever `.env` was absent, so in the default
runtime any caller who could reach `servicing-service:8002` could read that value
out of this repository and call `adjust-balance` or `apply-payment` for real.

The check ran, passed, and protected nothing — which is worse than no check,
because `ARCHITECTURE.md` and the PR both claimed the routes were defended.

The fallback is gone from compose (`${INTERNAL_SERVICE_TOKEN:?...}` now refuses to
interpolate), and this asserts the second half: that the value cannot come back
through some other path — an `.env` copied from an example, a Kubernetes manifest,
a developer exporting it out of habit — without the service refusing to start.

`test_the_compose_default_cannot_silently_return` is the one that matters most
over time. Removing a default from one file is easy; keeping it removed is the
part that fails quietly a year later.
"""
import pathlib

import pytest

from app import config


PROD_ENVS = ["", "production", "prod", "staging", "PRODUCTION"]
REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]


@pytest.mark.parametrize("env", PROD_ENVS)
@pytest.mark.parametrize("token", sorted(config.KNOWN_DEV_TOKENS))
def test_a_known_or_empty_token_refuses_to_boot(env, token):
    with pytest.raises(config.InsecureInternalTokenError) as excinfo:
        config.validate_internal_token(environment=env, token=token)

    message = str(excinfo.value)
    assert "INTERNAL_SERVICE_TOKEN" in message
    assert "ENVIRONMENT" in message, "the error must say what to set, not only that it failed"


@pytest.mark.parametrize("env", PROD_ENVS)
def test_a_real_secret_boots(env):
    config.validate_internal_token(environment=env, token="s3cret-from-a-vault-not-this-repo")


@pytest.mark.parametrize("env", ["development", "dev", "test", "local", "TEST"])
@pytest.mark.parametrize("token", ["", "dev-internal-token-change-me", "test-internal-token"])
def test_dev_environments_may_use_a_weak_token(env, token):
    """Local work and CI must stay possible. The rule is about deployments."""
    config.validate_internal_token(environment=env, token=token)


def test_an_unset_environment_is_treated_as_production():
    """The property most likely to be inverted by a later edit.

    A container boots with no ENVIRONMENT, so "unset" is a real reachable
    deployment state. Defaulting it to development would make this validator
    pass in exactly the situation it exists to catch.
    """
    with pytest.raises(config.InsecureInternalTokenError):
        config.validate_internal_token(environment="", token="dev-internal-token-change-me")


def test_the_compose_default_cannot_silently_return():
    """compose must not supply a token value of its own, for any service.

    Asserted against the file rather than against behaviour because the failure
    is a re-introduction: someone adds `:-something` back to make a local run
    easier, and every service silently accepts a published secret again.
    """
    compose = (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "dev-internal-token-change-me" not in compose, (
        "the repository-known token is back in docker-compose.yml"
    )
    assert "${INTERNAL_SERVICE_TOKEN:-" not in compose, (
        "compose supplies a default for INTERNAL_SERVICE_TOKEN again; a fallback "
        "committed to this repository is not a secret, so it must stay required "
        "(${INTERNAL_SERVICE_TOKEN:?...})"
    )
    assert compose.count("${INTERNAL_SERVICE_TOKEN:?") >= 1


def test_the_env_example_documents_the_requirement():
    """A required variable with no default needs somewhere to be discovered."""
    example = (REPO_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "INTERNAL_SERVICE_TOKEN" in example
    assert "secrets.token_urlsafe" in example, (
        "tell the operator how to generate one, or they will invent a weak value"
    )


# --- review round 2: a denylist is not a policy -------------------------------

@pytest.mark.parametrize("weak", ["1", "abc", "password", "hunter2", "a" * 31, "short-secret"])
def test_a_short_or_guessable_token_refuses_to_boot(weak):
    """A denylist can only reject the weak values someone thought of.

    Review round 2: `INTERNAL_SERVICE_TOKEN=1` booted cleanly, and because these
    routes carry no role or ownership check, a guessable token makes direct money
    movement brute-forceable the moment the network boundary fails -- which is
    the only scenario the token exists for.
    """
    with pytest.raises(config.InsecureInternalTokenError):
        config.validate_internal_token(environment="production", token=weak)


@pytest.mark.parametrize("padded", [
    "ChangeMe-padded-out-to-thirty-two-plus",
    "this-is-a-placeholder-value-really-long",
    "correct-horse-battery-password-stapler",
    "TODO-generate-a-real-secret-before-prod",
])
def test_a_long_placeholder_is_still_refused(padded):
    """Length alone is not entropy. A sentence is not a secret, however long."""
    assert len(padded) >= config.MIN_TOKEN_LENGTH
    with pytest.raises(config.InsecureInternalTokenError):
        config.validate_internal_token(environment="production", token=padded)


def test_a_generated_secret_is_accepted():
    """The policy must not reject the thing .env.example tells operators to use."""
    import secrets as _secrets
    config.validate_internal_token(environment="production", token=_secrets.token_urlsafe(32))


def test_the_error_says_how_to_generate_one():
    """An error that only says "no" gets worked around with a longer placeholder."""
    with pytest.raises(config.InsecureInternalTokenError) as excinfo:
        config.validate_internal_token(environment="production", token="1")
    assert "token_urlsafe" in str(excinfo.value)


def test_every_service_given_the_token_also_declares_its_environment():
    """A service that validates at startup must be told what environment it is in.

    This is the bug that reached CI. `servicing-service` received
    INTERNAL_SERVICE_TOKEN from compose but no ENVIRONMENT, and `validate_internal_token`
    treats an unset ENVIRONMENT as production -- correctly, since a container boots
    without one. So the service refused to start anywhere `.env` was absent.

    `.env` is gitignored, so it is present on a developer's machine and never in
    CI. The result was a change that booted locally, died in CI, and failed in a
    way that pointed nowhere near the cause: the loan page fetches
    `/lss/loans/{id}` before its schedule, so an unreachable servicing surfaced as
    "Amortization schedule heading not found" two files away.

    Asserted over the file for every service, not just this one, because the same
    omission in any of them produces the same silent startup failure -- and a
    hand-checked list is the thing that failed last time.
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

    def _validates_at_startup(service: str) -> bool:
        """Only services whose code actually calls the validator can be broken by
        a missing ENVIRONMENT. Derived from the source rather than listed, so a
        service that gains startup validation later is covered the same day --
        a hand-maintained list is precisely what failed here."""
        cfg = REPO_ROOT / "services" / service / "app" / "config.py"
        return cfg.is_file() and "def validate_internal_token" in cfg.read_text(encoding="utf-8")

    missing = [
        name for name, body in blocks.items()
        if "INTERNAL_SERVICE_TOKEN" in body
        and "ENVIRONMENT" not in body
        and _validates_at_startup(name)
    ]
    assert not missing, (
        f"these services receive INTERNAL_SERVICE_TOKEN but no ENVIRONMENT: {missing}. "
        "Startup validation treats an unset ENVIRONMENT as production, so each of "
        "them refuses to boot wherever .env is absent -- which is every environment "
        "except a developer's own machine."
    )
