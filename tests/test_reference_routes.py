# SPDX-License-Identifier: Apache-2.0
"""The HTTP API reference names every route the app serves, and no route it has stopped serving.

`docs/reference/http-api.md` is written by hand, from the code, because the OpenAPI schema carries
none of the behaviour a reader looks it up for. A hand-written list goes stale the first time a route
is added without it, so this test reads the app's routing table (through `scripts/list_routes.py`,
the same code an author runs) and fails on any difference: a route with no entry, a route missing
from the index table, or an entry for an `/api` route that no longer exists.

Building the app does not run its lifespan, so nothing here touches storage or a model.
"""

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "list_routes.py"
REFERENCE = ROOT / "docs" / "reference" / "http-api.md"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("list_routes", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_every_served_route_is_in_the_reference_and_nothing_stale_is(mod):
    served = {(method, path) for method, path, _ in mod.app_routes()}
    found = mod.problems(served, REFERENCE.read_text(encoding="utf-8"))
    assert found == [], (
        "docs/reference/http-api.md is out of step with the app:\n  " + "\n  ".join(found)
        + "\nAdd or remove the entry (a '### `METHOD /path`' heading and a route-index row)."
    )


def test_the_websocket_stream_is_listed_as_a_route(mod):
    """The OpenAPI schema omits WebSocket routes; the reference must not."""
    assert ("WS", "/api/runs/{ref}/stream") in {(m, p) for m, p, _ in mod.app_routes()}


def test_fastapi_documentation_routes_are_not_required(mod):
    paths = {p for _, p, _ in mod.app_routes()}
    assert not paths & {"/openapi.json", "/docs", "/redoc", "/docs/oauth2-redirect"}


def test_a_route_without_an_entry_is_reported(mod):
    found = mod.problems({("GET", "/api/widgets")}, "| `GET` | `/api/widgets` | x |\n")
    assert found == ["no entry for GET /api/widgets"]


def test_a_route_missing_from_the_index_is_reported(mod):
    found = mod.problems({("POST", "/api/widgets")}, "### `POST /api/widgets`\n")
    assert found == ["not in the route index: POST /api/widgets"]


def test_an_entry_for_a_removed_api_route_is_reported(mod):
    markdown = "### `DELETE /api/widgets/{id}`\n\n| `DELETE` | `/api/widgets/{id}` | x |\n"
    found = mod.problems(set(), markdown)
    assert "entry for a route the app does not serve: DELETE /api/widgets/{id}" in found
    assert "route index lists a route the app does not serve: DELETE /api/widgets/{id}" in found


def test_the_static_catch_all_may_be_documented_without_a_frontend_build(mod):
    """`/{full_path:path}` exists only when the SPA is built, so documenting it is not staleness."""
    markdown = "### `GET /{full_path:path}`\n\n| `GET` | `/{full_path:path}` | x |\n"
    assert mod.problems(set(), markdown) == []
