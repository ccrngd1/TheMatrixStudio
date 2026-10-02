#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""List the HTTP and WebSocket routes the app registers, and check them against the reference.

    scripts/list_routes.py                                # one route per line
    scripts/list_routes.py --markdown                     # a table, for pasting
    scripts/list_routes.py --check docs/reference/http-api.md

Builds the FastAPI app with `create_app()` and reads its routing table. It does not run the app's
lifespan, so nothing connects to storage, no model is called and nothing is written.

`--check` exits 1 when a route in the app has no entry in the reference, or when the reference has an
entry for an `/api` route (or `/config.json`) the app no longer serves. An entry is a heading of the
form "### `METHOD /path`" (WebSocket routes use `WS`), and the route-index table rows of the form
"| `METHOD` | `/path` |". FastAPI's own documentation routes and static mounts are not listed.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import Iterable, List, Set, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

Route = Tuple[str, str]

#: Routes FastAPI adds for its own documentation. They are not part of the product's API.
_FASTAPI_DOC_PATHS = {"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}

#: Paths the reverse check covers: a documented route outside these (the static SPA catch-all)
#: exists only when the frontend is built, so its absence from the app is not staleness.
_CHECKED_PREFIXES = ("/api", "/config.json")

_HEADING = re.compile(r"^###\s+`(GET|POST|PUT|PATCH|DELETE|WS)\s+(/[^`\s]*)`\s*$", re.MULTILINE)
_INDEX_ROW = re.compile(r"^\|\s*`(GET|POST|PUT|PATCH|DELETE|WS)`\s*\|\s*`(/[^`]*)`\s*\|", re.MULTILINE)


def app_routes(app=None) -> List[Tuple[str, str, str]]:
    """`(method, path, summary)` for every route the app serves, sorted by path then method."""
    from fastapi.routing import APIRoute, APIWebSocketRoute
    from starlette.routing import WebSocketRoute

    if app is None:
        from matrix_studio.api.app import create_app

        app = create_app()

    out: List[Tuple[str, str, str]] = []
    for route in app.routes:
        path = getattr(route, "path", "")
        if path in _FASTAPI_DOC_PATHS:
            continue
        if isinstance(route, APIRoute):
            doc = (route.endpoint.__doc__ or "").strip().splitlines()
            summary = doc[0].strip() if doc else ""
            for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
                out.append((method, path, summary))
        elif isinstance(route, (APIWebSocketRoute, WebSocketRoute)):
            out.append(("WS", path, ""))
    return sorted(out, key=lambda r: (r[1], r[0]))


def documented(markdown: str) -> Tuple[Set[Route], Set[Route]]:
    """`(headings, index_rows)`: the routes the reference has an entry for, and those its index lists."""
    return (
        {(m, p) for m, p in _HEADING.findall(markdown)},
        {(m, p) for m, p in _INDEX_ROW.findall(markdown)},
    )


def _checked(routes: Iterable[Route]) -> Set[Route]:
    return {r for r in routes if r[1].startswith(_CHECKED_PREFIXES)}


def problems(served: Set[Route], markdown: str) -> List[str]:
    """Every way the reference and the app disagree, as sentences. Empty when they agree."""
    headings, index = documented(markdown)
    found: List[str] = []
    for method, path in sorted(served - headings, key=lambda r: (r[1], r[0])):
        found.append(f"no entry for {method} {path}")
    for method, path in sorted(served - index, key=lambda r: (r[1], r[0])):
        found.append(f"not in the route index: {method} {path}")
    for method, path in sorted(_checked(headings) - served, key=lambda r: (r[1], r[0])):
        found.append(f"entry for a route the app does not serve: {method} {path}")
    for method, path in sorted(_checked(index) - served, key=lambda r: (r[1], r[0])):
        found.append(f"route index lists a route the app does not serve: {method} {path}")
    return found


def main(argv: List[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="List the app's routes, or check them against the HTTP API reference.")
    ap.add_argument("--markdown", action="store_true", help="Print a Markdown table instead of plain lines.")
    ap.add_argument("--check", metavar="FILE", help="Compare against this reference file; exit 1 on any difference.")
    args = ap.parse_args(argv)

    routes = app_routes()
    if args.check:
        found = problems({(m, p) for m, p, _ in routes}, Path(args.check).read_text(encoding="utf-8"))
        for line in found:
            print(line)
        if found:
            return 1
        print(f"{len(routes)} routes, all documented in {args.check}")
        return 0
    if args.markdown:
        print("| Method | Path | Summary |")
        print("|---|---|---|")
        for method, path, summary in routes:
            print(f"| `{method}` | `{path}` | {summary} |")
        return 0
    for method, path, summary in routes:
        print(f"{method:<7} {path}" + (f"  {summary}" if summary else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
