"""What the local stack exposes to the machine it runs on, and to the network.

Two findings from the pre-freeze sweep, both about the same line of YAML:

  * Postgres published `"5432:5432"`, which binds 0.0.0.0 -- every interface, not
    just loopback.
  * Redis published `"6379:6379"` on every interface too, while having no
    authentication at all (SEC-09) and holding the session store the gateway
    resolves identity and role from.

Neither is a code defect and neither is production security. What they are is a
local topology that exposed more than any caller needed: on a laptop behind a
firewall the difference is invisible, and on conference wifi or a client site it
is the difference between "a demo" and "an unauthenticated session store other
machines can reach".

This guard holds the shape rather than the exact string, and it is written to fail
LOUDLY toward the reason rather than toward the syntax -- a reader who trips it
should learn why the binding matters, not just that a test wants a prefix.
"""
import pathlib
import re

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]
COMPOSE = REPO / "docker-compose.yml"
COMPOSE_E2E = REPO / "docker-compose.e2e.yml"

#: Services whose ports may be published on every interface, and why.
#:
#: The application surface a person opens in a browser. Publishing these broadly
#: is what makes the demo work from another device on the same network, which is
#: a legitimate thing to want and is a different question from a database port.
_APP_SURFACE = {"gateway", "frontend", "prometheus", "grafana"}

#: The data stores this change is about, and the rule each one is held to.
#:
#: `None` means "must not be published to the host at all"; a string means "must
#: be published, and every entry must start with this".
_DATA_STORES = {
    "postgres": "127.0.0.1:",
    "redis": None,
}


def _publication_is_allowed(service: str, entry: str) -> str | None:
    """The single rule. Returns None when the entry is fine, else why it is not.

    Both the base-file tests and the overlay test call this, so the two files
    cannot end up held to different standards -- which is precisely what happened
    when the overlay checked Redis for a loopback prefix while the base file
    required no publication at all.
    """
    required = _DATA_STORES[service]
    if required is None:
        return (
            f"{service} is an unauthenticated session store that nothing on the "
            "host connects to -- a repository-wide search for localhost:6379 "
            "returns nothing -- so the rule is no host publication at all, on any "
            "interface, loopback included. Remove it rather than binding it.")
    if not entry.startswith(required):
        return (
            f"{service} publishes {entry!r}, which binds every interface. On any "
            "shared network that makes the database reachable from other machines. "
            f'Use "{required}5432:5432".')
    return None


def _service_ports(text: str) -> dict[str, list[str]]:
    """service name -> published port strings, read out of the compose file.

    Deliberately a small parser rather than a YAML load: `docker compose config`
    would need the interpolation variables this file refuses to default, and the
    structure being asserted is two levels deep and stable.
    """
    services: dict[str, list[str]] = {}
    current = None
    in_ports = False
    for raw in text.splitlines():
        if re.match(r"^  [a-z0-9_-]+:\s*$", raw):
            current = raw.strip().rstrip(":")
            services.setdefault(current, [])
            in_ports = False
            continue
        if current is None:
            continue
        if re.match(r"^    ports:\s*$", raw):
            in_ports = True
            continue
        if in_ports:
            line = raw.split("#", 1)[0].rstrip()
            if not line.strip():
                continue
            m = re.match(r"^\s+-\s+(.*)$", line)
            if m:
                item = m.group(1).strip()
                if item.startswith(('"', "'")) and item[0] == item[-1:]:
                    item = item[1:-1]
                elif ": " in item or item.endswith(":"):
                    # Long syntax (`- target: 6379` / `host_ip: 127.0.0.1`). This
                    # parser cannot read it, and the honest failure is to say so
                    # rather than to return nothing and let the rule pass on a
                    # publication it simply could not see. A short-syntax-only
                    # guard is exactly the shape of hole MIN-01 described.
                    item = f"<long syntax this guard cannot read: {item}>"
                services[current].append(item)
            elif not line.startswith("      "):
                in_ports = False
    return services


def test_postgres_is_published_to_loopback_only():
    """It must stay published -- and must not be reachable from another host.

    `db/tests` and the browser suite both connect from the host, and CI's e2e job
    uses `postgresql://...@localhost:5432/meridian` against this stack, so removing
    the publication would break them. Binding it to 127.0.0.1 keeps every one of
    those callers working and removes reachability from anywhere else.
    """
    ports = _service_ports(COMPOSE.read_text(encoding="utf-8"))
    published = ports.get("postgres", [])
    assert published, (
        "postgres no longer publishes a port; db/tests and the e2e job connect "
        "from the host and will fail")
    for entry in published:
        problem = _publication_is_allowed("postgres", entry)
        assert problem is None, problem


def test_redis_is_not_published_to_the_host_at_all():
    """Nothing outside the compose network connects to it, and it has no password.

    Redis holds `session:<uuid4>` keys, and the gateway resolves a caller's
    identity and role from them. It has no authentication (SEC-09). Publishing it
    -- on any interface, loopback included -- is exposure nothing needs: every
    service reaches it as `redis:6379` over the compose network, and
    `docker compose exec redis redis-cli` still works for debugging.
    """
    ports = _service_ports(COMPOSE.read_text(encoding="utf-8"))
    published = ports.get("redis", [])
    assert published == [], _publication_is_allowed("redis", published[0])


def test_nothing_on_the_host_actually_needs_redis():
    """The premise the removal rests on, checked rather than asserted.

    If some host-side tool ever does need Redis, this fails first and says so --
    at which point the answer is a loopback binding, not a return to 0.0.0.0.
    """
    hits = []
    for path in REPO.rglob("*"):
        if not path.is_file():
            continue
        parts = path.parts
        if "node_modules" in parts or ".git" in parts or "__pycache__" in parts:
            continue
        if path.suffix not in {".py", ".ts", ".tsx", ".yml", ".yaml", ".sh", ".json"}:
            continue
        if path.resolve() == pathlib.Path(__file__).resolve():
            # This guard names the address in order to forbid it.
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:                                  # pragma: no cover
            continue
        # Comments explain WHY the publication was removed and name the address
        # while doing so. What is being looked for is a caller, not a mention.
        code = re.sub(r"^\s*#.*$", "", text, flags=re.MULTILINE)
        code = re.sub(r"^\s*//.*$", "", code, flags=re.MULTILINE)
        if re.search(r"(?:localhost|127\.0\.0\.1):6379", code):
            hits.append(path.relative_to(REPO).as_posix())
    assert not hits, (
        f"something now connects to Redis from the host: {hits}. Publish it on "
        "127.0.0.1 only -- never on 0.0.0.0 -- and update the guard above.")


@pytest.mark.parametrize("service", sorted(_APP_SURFACE))
def test_the_application_surface_is_left_alone(service):
    """This change is scoped to the data stores.

    The gateway, the frontend and the dashboards are the demo surface, and whether
    they should be reachable from another device is a product question rather than
    a hygiene one. Asserting they are UNCHANGED keeps the scope of this PR honest
    and stops a later "tighten everything" pass being read into it.
    """
    ports = _service_ports(COMPOSE.read_text(encoding="utf-8"))
    for entry in ports.get(service, []):
        assert not entry.startswith("127.0.0.1:"), (
            f"{service} was bound to loopback by a change that claimed to cover "
            "only the data stores")


@pytest.mark.parametrize("service", sorted(_DATA_STORES))
def test_the_e2e_overlay_does_not_republish_them(service):
    """The overlay raises the gateway rate limit and must not undo this.

    Compose MERGES `ports` across files rather than replacing them, so an entry
    added here is added to whatever the base file publishes. The overlay is
    therefore held to the SAME rule as the base file, service by service, through
    the same `_publication_is_allowed` function -- an earlier version of this test
    checked both stores for a loopback prefix, which would have let a future
    overlay publish `127.0.0.1:6379:6379` and put the unauthenticated session
    store back on the host while the base test still passed. Two rules for one
    invariant is how that gap opened; there is now one.
    """
    if not COMPOSE_E2E.exists():                          # pragma: no cover
        pytest.skip("no e2e overlay in this checkout")
    ports = _service_ports(COMPOSE_E2E.read_text(encoding="utf-8"))
    for entry in ports.get(service, []):
        problem = _publication_is_allowed(service, entry)
        assert problem is None, (
            f"the e2e overlay publishes {service} as {entry!r}, and compose merges "
            f"that onto the base file rather than replacing it. {problem}")


def test_the_overlay_rule_is_the_same_rule_as_the_base_file():
    """Anti-drift, asserted rather than trusted.

    The whole point of routing both tests through one function is that they cannot
    disagree. This pins the rule itself: a loopback Redis publication -- the exact
    shape MIN-01 said would slip through -- must be REFUSED, and refused with the
    same verdict no matter which file it appears in.
    """
    assert _publication_is_allowed("redis", "127.0.0.1:6379:6379") is not None, (
        "a loopback Redis publication is being allowed. Redis is unauthenticated "
        "and nothing on the host connects to it, so the rule is no publication at "
        "all -- not a narrower one")
    assert _publication_is_allowed("redis", "6379:6379") is not None
    assert _publication_is_allowed("postgres", "5432:5432") is not None, (
        "an all-interfaces Postgres publication is being allowed")
    assert _publication_is_allowed("postgres", "127.0.0.1:5432:5432") is None, (
        "the loopback Postgres publication db/tests and the e2e job depend on is "
        "being refused")


# --------------------------------------------------------------------------
# The parser itself, because a guard that cannot SEE a publication reports the
# same "clean" as one that saw nothing published. Compose accepts short syntax
# quoted and unquoted, and a long mapping form; the original parser matched only
# the quoted short form, so `- 6379:6379` written without quotes would have been
# invisible and the Redis rule would have passed on a published Redis.
# --------------------------------------------------------------------------

_UNQUOTED = """services:
  redis:
    image: redis:7
    ports:
      - 6379:6379
"""

_LONG_FORM = """services:
  redis:
    image: redis:7
    ports:
      - target: 6379
        published: 6379
        host_ip: 127.0.0.1
"""

_TRAILING_COMMENT = """services:
  postgres:
    ports:
      - "127.0.0.1:5432:5432"   # loopback only
"""


def test_an_unquoted_publication_is_still_seen():
    assert _service_ports(_UNQUOTED)["redis"] == ["6379:6379"]
    assert _publication_is_allowed("redis", "6379:6379") is not None


def test_a_long_form_publication_fails_loudly_instead_of_silently():
    """Not parsed -- and therefore not passed."""
    entries = _service_ports(_LONG_FORM)["redis"]
    assert entries, "a long-syntax publication vanished from the parser entirely"
    assert all(_publication_is_allowed("redis", e) is not None for e in entries)
    assert any(_publication_is_allowed("postgres", e) is not None for e in entries), (
        "a long-syntax entry passed the postgres rule; this parser cannot read "
        "long syntax, so it must refuse rather than approve it")


def test_a_trailing_comment_does_not_hide_the_entry():
    assert _service_ports(_TRAILING_COMMENT)["postgres"] == ["127.0.0.1:5432:5432"]


def test_the_real_files_use_syntax_this_parser_can_read():
    """The premise every other test here rests on.

    If either compose file ever adopts long syntax, this says so directly instead
    of every rule above quietly reporting a clean stack.
    """
    for path in (COMPOSE, COMPOSE_E2E):
        if not path.exists():                             # pragma: no cover
            continue
        for service, entries in _service_ports(path.read_text(encoding="utf-8")).items():
            for entry in entries:
                assert not entry.startswith("<long syntax"), (
                    f"{path.name} publishes {service} using long syntax, which this "
                    "guard cannot read. Teach the parser before relying on it.")
