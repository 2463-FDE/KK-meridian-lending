"""A route the model card advertises must actually be served.

`docs/model_card.md` sends an auditor to a specific staff endpoint as evidence
that the ZIP outcome screen exists. The first version of that guard (PR #62)
grepped `applications.py` for `@router.get("...")` — which proves TEXT, not a
served route. A commented-out decorator, a docstring, or an unrelated string
literal would have satisfied it while FastAPI returned 404. Reviewed as MC-004.

This asks the application instead. `app.routes` is what Uvicorn serves, so a
route present here is a route a caller can reach, and one absent here is a 404
no matter what the source file still contains.

It lives in origination-service's suite rather than `db/tests` for a plain
reason: this is where the app and its dependencies are installed. A test in
`db/tests` would have to add the service to `sys.path` and import FastAPI,
LangGraph and the rest — and would skip or error in the db job, which is the
"skip reads like a pass" failure this repository keeps finding.

Method matters as much as path. The card advertises a GET; a route registered
for POST only is a different contract and a 405 to the auditor following it.
"""
import pathlib
import re

import pytest
from fastapi.routing import APIRoute

from app.main import app

CARD = pathlib.Path(__file__).resolve().parents[3] / "docs" / "model_card.md"

#: `GET /applications/fair-lending/zip-analysis` as the card writes it.
_ADVERTISED = re.compile(r"`(GET|POST|PUT|PATCH|DELETE) (/[A-Za-z0-9/_{}-]+)`")


#: A route the card names as GONE is not advertised, it is recorded.
#:
#: The ZIP3 disparate-impact route was retired on 2026-08-24 when the client
#: prohibited ZIP/ZIP3 as a protected-class proxy, and the card still names it so
#: a reader can see what was removed and why. Demanding that every route written
#: in the card be served would force the card to either stop explaining the
#: removal or start advertising a 404 -- so a line that says the route is gone is
#: read as history, on the same line, the way the citation guards do it.
_RETIRED_ON_THIS_LINE = re.compile(
    r"no longer registered|retired|deleted|not on `main`|removed", re.I)


def _advertised_routes():
    if not CARD.is_file():
        return []

    advertised = []
    for line in CARD.read_text(encoding="utf-8").splitlines():
        for method, path in _ADVERTISED.findall(line):
            if _RETIRED_ON_THIS_LINE.search(line):
                continue
            advertised.append((method, path))
    return advertised


def _registered():
    """(method, path) for every route this app actually serves."""
    pairs = set()
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods:
                pairs.add((method.upper(), route.path))
    return pairs


def test_the_card_advertises_at_least_one_route():
    """Guard the guard.

    A card that stopped advertising routes, or a pattern that stopped matching
    them, would make the parametrized test below vacuous — it would pass by
    having nothing to check, which is the same shape as the defect it replaced.
    """
    assert _advertised_routes(), (
        f"{CARD.name} advertises no route, or the pattern no longer matches how "
        f"it writes them"
    )


def test_the_registered_route_set_is_populated():
    """If the app exposed nothing, every membership assertion would fail loudly
    rather than pass — but an empty set would also mean the import gave us
    something that is not the real app."""
    registered = _registered()

    assert len(registered) > 10, f"only {len(registered)} routes registered"
    assert any(path.startswith("/applications") for _m, path in registered)


@pytest.mark.parametrize("method,path", _advertised_routes())
def test_every_route_the_model_card_advertises_is_served(method, path):
    """The card is the only place this endpoint is advertised to an auditor."""
    registered = _registered()

    assert (method, path) in registered, (
        f"the model card advertises {method} {path}, which this app does not "
        f"serve. An auditor following the card gets a 404, and the governance "
        f"artefact is describing a control that is not reachable. Registered "
        f"paths under that prefix: "
        f"{sorted(p for m, p in registered if p.startswith(path.rsplit('/', 1)[0]))}"
    )


def test_a_decorator_in_a_comment_would_not_satisfy_this():
    """The regression MC-004 names, stated as an executable difference.

    Source text and served routes are different things. This asserts the second
    is what the check reads: a path that appears in the module's source but is
    not registered must not be in the registered set.
    """
    source = (pathlib.Path(__file__).resolve().parents[1]
              / "app" / "routers" / "applications.py").read_text(encoding="utf-8")
    registered_paths = {p for _m, p in _registered()}

    invented = "/applications/fair-lending/not-a-real-route"
    assert invented not in source, "fixture path unexpectedly present in the source"
    assert invented not in registered_paths
