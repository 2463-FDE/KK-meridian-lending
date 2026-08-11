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
    config.validate_internal_token(environment=env, token="a-real-secret-from-a-vault")


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
