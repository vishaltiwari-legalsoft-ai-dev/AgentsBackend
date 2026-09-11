"""Audience scope — what an account limited to the GEO workspace may reach.

Why this file exists
--------------------
"GEO panel only" was, until this file, a sentence people said rather than a
thing the service did. ``is_geo_editor`` is an ADDITIVE flag: it opens nine
registry-shaping routes on top of everything the account already reached, and
what every signed-in account already reached was the whole workspace, because
the base guard on every other route is ``get_current_user`` — a sign-in
allowlist check with no concept of roles at all. Of 168 authenticated routes a
"GEO editor" got their 9 and 122 non-GEO routes nobody ever meant them to have.

That was not theoretical. Four of the eight are outside contractors, and two
requests — ``POST /api/mr/ask`` and ``POST /api/mr/ingest-sheet`` — read the
whole company marketing tracker through a shared service account with the
caller's identity nowhere in the path.

The shape of the fix
--------------------
A scope, not another flag, and default-deny.

:data:`GEO_SCOPE_ROUTES` is the entire set of ``(method, path)`` a GEO-only
account may reach. :func:`deny_outside_geo` is attached ONCE, in
``app.main``'s ``include_router`` loop, to every router the service mounts —
never per handler. That ordering matters more than it looks: a per-handler
guard is a thing somebody has to remember on the day they add route 169, and
"somebody forgot" is precisely how the first version of this went wrong. Here
the wall is applied to the router, so a new route is INSIDE it by construction
and has to be typed into the table below to get out.

The wall narrows; it never authenticates
----------------------------------------
:func:`deny_outside_geo` resolves its principal through
``security.optional_principal``, so a request with no usable token passes
straight through to whatever guard the route already had. That is deliberate:
the same dependency sits on ``/api/health``, ``/api/auth/google`` and the cron
routes, and turning any of those into authenticated endpoints would be a much
larger change than this one. Nothing here makes anything reachable — it only
takes reach away, and only from accounts named in ``GEO_ONLY_EMAILS``.
"""
from __future__ import annotations

import logging

from fastapi import Depends, HTTPException, Request, status

from app.security import optional_principal

logger = logging.getLogger("agentos.scopes")

#: Refusal shown to a GEO-only caller. Says what happened and nothing about the
#: route, the workspace or why it exists — the same discipline as the sign-in
#: refusal in ``routers.auth``.
SCOPE_REFUSED = "This account is limited to the GEO workspace."

#: The ONE app-level route: ``GET /`` is declared on the ``FastAPI`` object in
#: ``app.main``, not through a router, so the include-time dependency below
#: cannot reach it. It is a static service banner (name + two doc links) and
#: carries no data, so leaving it open costs nothing — but it is pinned here
#: rather than forgotten, because the same gap would swallow a real route.
#: ``test_every_route_sits_behind_the_scope_wall`` fails if a second one ever
#: appears: new routes go in a router.
UNREACHABLE_BY_THE_WALL: frozenset[tuple[str, str]] = frozenset({("GET", "/")})

#: Every ``(method, path)`` a GEO-only account may reach — the sign-in door,
#: the shell that has to render before the GEO workspace is openable, and the
#: GEO workspace itself. Everything else in the service answers 403.
#:
#: Enumerated by READING THE FRONTEND, not by guessing from route names: the
#: GEO screens under ``newfrontend/components/hub/work/geo/*``, the workspace
#: that hosts them, and everything ``HubApp`` fires on mount before any of it
#: is reachable. A route that is not on one of those paths is not here, and a
#: route that is on one of them is here even when it belongs to another agent
#: — which is why ``GET /api/seo-geo/overview`` and ``GET /api/library`` are
#: below and flagged as such.
#:
#: Paths are FastAPI's templates including the ``/api`` prefix, matched against
#: ``request.scope["route"].path``, so ``{brand_id}`` here matches every brand
#: and a typo matches nothing — which fails closed, in the safe direction.
GEO_SCOPE_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        # --- the door and the liveness probe ------------------------------- #
        # Public already; listed so a GEO-only caller who happens to hold a
        # token is not refused something an anonymous stranger is served.
        ("GET", "/"),
        ("GET", "/api/health"),
        ("POST", "/api/auth/google"),
        # --- the shell, before any workspace is reachable ------------------ #
        # ``useShellStats`` fires all four on mount, for the rail counts and the
        # announcements dot. Each one's failure is swallowed by the console, so
        # a 403 here would be invisible on screen and loud in the network log —
        # the worst of both. They are in scope on purpose.
        #
        # ``GET /api/library`` is the one entry here that is not GEO's and not
        # the caller's own: it lists the Graphics Designer creative gallery, and
        # the rail wants one number off it. It is a read of shared brand
        # creatives, not of anyone's account data, and it is the narrowest thing
        # that keeps the shell honest — but it IS reach outside GEO, and the
        # clean removal is a console change (drop the count for a scoped user),
        # not a backend one.
        ("GET", "/api/library"),
        ("GET", "/api/issues"),
        ("GET", "/api/news"),
        # The caller's OWN runs. ``firestore_repo.list_runs_for_user`` filters
        # on ``user_id`` before it orders, so this is the one route here that is
        # already tenant-scoped: a GEO-only account sees its own GEO runs and
        # nothing else, which is what the rail count, the Home ledger, the Runs
        # panel and the Agents panel all read.
        ("GET", "/api/runs"),
        # --- Home, and the GEO Brands panel -------------------------------- #
        # The second non-GEO read, and the only one the GEO workspace itself
        # needs: ``geo/Brands.tsx`` reads the switched-OFF brands from here
        # (``brand.enabled === false``), because the GEO brand list serves only
        # enabled ones and "switch it back on" needs to see the ones that are
        # off. ``HomeView`` reads it too. It serves every brand's SEO run
        # summary, so it is real reach outside GEO — recorded here rather than
        # smuggled in, and the console-side fix is noted in the handover.
        ("GET", "/api/seo-geo/overview"),
        # --- the GEO workspace --------------------------------------------- #
        # The whole ``/api/geo`` router except its cron endpoint, which is
        # driven by the scheduler with a shared key and is nobody's UI.
        ("GET", "/api/geo/config"),
        ("GET", "/api/geo/brands"),
        ("POST", "/api/geo/brands"),
        ("GET", "/api/geo/brands/{brand_id}/answers"),
        ("GET", "/api/geo/brands/{brand_id}/comparison"),
        ("GET", "/api/geo/brands/{brand_id}/config"),
        ("PUT", "/api/geo/brands/{brand_id}/config"),
        ("GET", "/api/geo/brands/{brand_id}/history"),
        ("PUT", "/api/geo/brands/{brand_id}/personas"),
        ("POST", "/api/geo/brands/{brand_id}/page-check"),
        ("GET", "/api/geo/brands/{brand_id}/page-checks"),
        ("GET", "/api/geo/brands/{brand_id}/page-checks/{check_id}"),
        ("POST", "/api/geo/brands/{brand_id}/page-checks/{check_id}/rescore"),
        ("GET", "/api/geo/brands/{brand_id}/poll/status"),
        ("POST", "/api/geo/brands/{brand_id}/poll/step"),
        ("GET", "/api/geo/brands/{brand_id}/prompts"),
        ("PUT", "/api/geo/brands/{brand_id}/prompts"),
        ("POST", "/api/geo/brands/{brand_id}/prompts/bulk"),
        ("POST", "/api/geo/brands/{brand_id}/prompts/custom"),
        ("POST", "/api/geo/brands/{brand_id}/prompts/generate"),
        ("GET", "/api/geo/brands/{brand_id}/report"),
        ("POST", "/api/geo/brands/{brand_id}/rescan"),
        ("GET", "/api/geo/brands/{brand_id}/strategy"),
        ("POST", "/api/geo/brands/{brand_id}/strategy/generate"),
        ("PUT", "/api/geo/brands/{brand_id}/strategy/actions/{action_id}"),
    }
)


def route_key(request: Request) -> tuple[str, str] | None:
    """``(METHOD, path template)`` for the route this request matched.

    Starlette puts the matched ``APIRoute`` on the scope before dependencies
    run, so this is the template (``/api/geo/brands/{brand_id}/report``) rather
    than the concrete URL — which is what makes a table of 33 entries able to
    describe a workspace with unbounded brand ids.

    ``None`` when there is no route on the scope, which should not happen
    inside a route's own dependency. The caller treats it as "unknown", and
    unknown is refused.
    """
    path = getattr(request.scope.get("route"), "path", None)
    if not path:
        return None
    return (request.method.upper(), str(path))


def deny_outside_geo(
    request: Request,
    principal: dict[str, object] | None = Depends(optional_principal),
) -> None:
    """Refuse a GEO-only account anything outside :data:`GEO_SCOPE_ROUTES`.

    Attached at ``include_router`` time in ``app.main``, to every router, which
    is the only placement that makes the next route added default to CLOSED.
    Per-handler is how this was got wrong the first time.

    Three ways to pass, and only three:

    * no usable token — nothing to narrow, and the route's own guard still runs;
    * a principal who is not GEO-only — every existing account, unchanged;
    * a GEO-only principal on a route in the table.

    Everything else is 403. A route the table does not mention is refused, so
    adding a route is never accidentally adding reach.
    """
    if principal is None or not principal.get("is_geo_only"):
        return

    key = route_key(request)
    if key is not None and key in GEO_SCOPE_ROUTES:
        return

    # Logged at the boundary with the two facts an operator needs — who, and
    # what they reached for — because the first question after "why did this
    # 403" is always one of those. The caller is told neither.
    logger.warning(
        "scope refusal: %s reached for %s (limited to the GEO workspace)",
        principal.get("email"),
        key or request.url.path,
    )
    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=SCOPE_REFUSED)
