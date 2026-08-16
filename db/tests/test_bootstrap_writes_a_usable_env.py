"""`make bootstrap` must produce an `.env` that `docker compose` can read.

That is the whole contract of the documented first command a developer runs, and
CI's `quick-start` job exists to prove it on a clean checkout.

It broke in a way worth pinning. The principal keys are PEMs -- multi-line -- and
`.env` is line-based, so bootstrap writes them with their newlines escaped. But
`_set_if_missing` filled an existing empty key using `re.sub` with a STRING
replacement, and `re.sub` expands escape sequences in a string replacement. The
escaped `\n` came back out as a real newline, `.env` gained a stray line, and
compose refused the whole file with:

    unexpected character "+" in variable name "MC4CAQAwBQYDK2VwBCIEIMVPI7..."

The subtle part, and the reason for this file: **it only bit on the clean-checkout
path.** `re.sub` runs when the key is present-but-empty, which is what copying
`.env.example` produces. A developer whose `.env` already carried the key took
the append branch, where nothing re-interprets the value. So it passed on the
machine that wrote it and failed in CI, on the one job whose purpose is proving a
clean checkout can start the stack.

Needs no database and no Docker: it runs the real bootstrap against a temporary
directory and reads what it wrote.
"""
import importlib.util
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
BOOTSTRAP = REPO / "scripts" / "bootstrap_env.py"
BACKSLASH_N = chr(92) + "n"


@pytest.fixture
def bootstrap(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("bootstrap_under_test", BOOTSTRAP)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setattr(module, "ENV", tmp_path / ".env")
    monkeypatch.setattr(module, "EXAMPLE", tmp_path / ".env.example")
    return module


def _clean_checkout(bootstrap):
    """What a fresh clone looks like: no .env, an example with empty keys."""
    bootstrap.EXAMPLE.write_text(
        "INTERNAL_SERVICE_TOKEN=\nPRINCIPAL_SIGNING_KEY=\nPRINCIPAL_VERIFY_KEY=\n",
        encoding="utf-8",
    )
    bootstrap.main()
    return bootstrap.ENV.read_text(encoding="utf-8")


@pytest.mark.parametrize("key", ["PRINCIPAL_SIGNING_KEY", "PRINCIPAL_VERIFY_KEY"])
def test_a_generated_key_occupies_exactly_one_line(bootstrap, key):
    env = _clean_checkout(bootstrap)
    match = re.search(rf"^{key}=(.+)$", env, re.M)
    assert match, f"bootstrap did not write {key}"
    value = match.group(1)
    assert "BEGIN" in value and "END" in value, (
        f"{key} was truncated at the first newline -- the escape was expanded, so "
        f"the rest of the PEM is now stray lines that compose cannot parse"
    )
    assert BACKSLASH_N in value, f"{key} is not newline-escaped"


def test_the_file_has_no_stray_pem_lines(bootstrap):
    """The failure as compose saw it: a base64 body sitting on its own line."""
    env = _clean_checkout(bootstrap)
    for number, line in enumerate(env.splitlines(), start=1):
        if not line or line.startswith("#"):
            continue
        assert "=" in line, (
            f"line {number} of the generated .env is not an assignment: {line[:60]!r}. "
            f"A PEM leaked across lines -- compose refuses the entire file."
        )


def test_filling_an_empty_key_does_not_reinterpret_the_value(bootstrap):
    """The exact branch that broke, isolated from the rest of bootstrap."""
    value = "-----BEGIN X-----" + BACKSLASH_N + "BODY+SLASH/" + BACKSLASH_N + "-----END X-----"
    text, changed = bootstrap._set_if_missing("PRINCIPAL_SIGNING_KEY=\n",
                                              "PRINCIPAL_SIGNING_KEY", value)
    assert changed
    assert text.strip() == f"PRINCIPAL_SIGNING_KEY={value}", (
        "the replacement was re-interpreted on the way in"
    )
    assert len(text.strip().splitlines()) == 1


def test_an_existing_value_is_never_overwritten(bootstrap):
    """Bootstrap is safe to re-run: it must not rotate a key someone is using,
    or a stack would start refusing every money route mid-session."""
    bootstrap.EXAMPLE.write_text("PRINCIPAL_SIGNING_KEY=\n", encoding="utf-8")
    bootstrap.ENV.write_text("PRINCIPAL_SIGNING_KEY=already-set\n", encoding="utf-8")
    bootstrap.main()
    assert "PRINCIPAL_SIGNING_KEY=already-set" in bootstrap.ENV.read_text(encoding="utf-8")
