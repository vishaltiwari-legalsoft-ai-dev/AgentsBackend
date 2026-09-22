"""Enumerate every route the app serves, with its full mounted path.

How ``app.routes`` presents an ``include_router(prefix="/api")`` mount is a
FastAPI version detail, and it changed underneath this service once already —
expensively. Older releases flattened the include: the prefix was baked into
each ``APIRoute.path`` and ``app.routes`` was one flat list. Newer releases
mount the router wrapped (an ``_IncludedRouter`` carrying ``original_router``
and an ``include_context`` with the prefix), so the top level holds no
``APIRoute`` at all and the sub-routes keep their unprefixed templates.

Every sweep-style test in this repo (route ledgers, allowlist sweeps, cron
registry conformance) filters ``app.routes`` for ``APIRoute`` — which under
the wrapped shape silently finds NOTHING, and a sweep that finds nothing
passes on nothing. This helper is the one copy of the walk that understands
both shapes, so the sweeps stay non-vacuous across upgrades. ``app.scopes``
solves the same version split for a single matched request, on its own terms
(the matched route rides on the request scope there; nothing is enumerated).
"""
from __future__ import annotations

from typing import Iterator

from fastapi.routing import APIRoute


def iter_api_routes(app_or_router, prefix: str = "") -> Iterator[tuple[str, APIRoute]]:
    """Yield ``(full_path, route)`` for every ``APIRoute`` the app serves.

    ``full_path`` is the path the service actually publishes — mount prefixes
    included — regardless of which shape ``app.routes`` takes. Non-APIRoute
    entries that are not router mounts (the docs pages, ``Mount`` for static
    files) are skipped, exactly as every caller's ``isinstance`` filter did.
    """
    for route in app_or_router.routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
            continue
        inner = getattr(route, "original_router", None)
        if inner is not None:
            ctx = getattr(route, "include_context", None)
            yield from iter_api_routes(inner, prefix + (getattr(ctx, "prefix", "") or ""))


def iter_api_routes_with_mount_deps(
    app_or_router, prefix: str = "", mount_deps: tuple = ()
) -> Iterator[tuple[str, APIRoute, tuple]]:
    """Like :func:`iter_api_routes`, plus the include-time dependency callables.

    The flattened shape merged ``include_router(dependencies=[...])`` into each
    route's ``dependant``; the wrapped shape keeps them on the include context
    and applies them at request time, so a sweep that walks ``route.dependant``
    for an include-time guard (the GEO scope wall) finds nothing there. The
    third element is the tuple of dependency callables accumulated from every
    enclosing include, in both shapes (empty under the flattened one, where
    they already sit in ``dependant``) — a caller checks both places.
    """
    for route in app_or_router.routes:
        if isinstance(route, APIRoute):
            yield prefix + route.path, route, mount_deps
            continue
        inner = getattr(route, "original_router", None)
        if inner is not None:
            ctx = getattr(route, "include_context", None)
            extra = tuple(
                dep.dependency
                for dep in (getattr(ctx, "dependencies", None) or ())
                if getattr(dep, "dependency", None) is not None
            )
            yield from iter_api_routes_with_mount_deps(
                inner,
                prefix + (getattr(ctx, "prefix", "") or ""),
                mount_deps + extra,
            )
