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
            m = re.match(r'^\s+-\s+"([^"]+)"\s*$', raw)
            if m:
                services[current].append(m.group(1))
            elif raw.strip() and not raw.strip().startswith("#"):
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
        assert entry.startswith("127.0.0.1:"), (
            f"postgres publishes {entry!r}, which binds every interface. On any "
            "shared network that makes the database reachable from other "
            'machines. Use "127.0.0.1:5432:5432".')


def test_redis_is_not_published_to_the_host_at_all():
    """Nothing outside the compose network connects to it, and it has no password.

    Redis holds `session:<uuid4>` keys, and the gateway resolves a caller's
    identity and role from them. It has no authentication (SEC-09). Publishing it
    -- on any interface, loopback included -- is exposure nothing needs: every
    service reaches it as `redis:6379` over the compose network, and
    `docker compose exec redis redis-cli` still works for debugging.
    """
    ports = _service_ports(COMPOSE.read_text(encoding="utf-8"))
    assert ports.get("redis", []) == [], (
        f"redis publishes {ports.get('redis')!r}. It is an unauthenticated "
        "session store and nothing on the host connects to it -- a repository-wide "
        "search for localhost:6379 returns nothing. Remove the publication rather "
        "than binding it.")


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


def test_the_e2e_overlay_does_not_republish_them():
    """The overlay raises the gateway rate limit and must not undo this."""
    if not COMPOSE_E2E.exists():                          # pragma: no cover
        pytest.skip("no e2e overlay in this checkout")
    ports = _service_ports(COMPOSE_E2E.read_text(encoding="utf-8"))
    for service in ("postgres", "redis"):
        for entry in ports.get(service, []):
            assert entry.startswith("127.0.0.1:"), (
                f"the e2e overlay republishes {service} as {entry!r}, undoing the "
                "base file's binding")
