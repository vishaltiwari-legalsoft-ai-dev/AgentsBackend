"""Structural tenancy conformance for the whole route table.

Why this file exists
--------------------
The per-agent cross-tenant suites in this directory (``test_gd_cross_tenant``,
``test_mr_cross_tenant``, ``test_blog_writer_cross_tenant``,
``test_creative_agent_cross_tenant``) are excellent at what they do, and they
share one blind spot: **they are hand-enumerated**. A suite exists because
somebody sat down and wrote it for the agent they had just built. So the
question "is endpoint X scoped?" is only ever asked about endpoints somebody
thought to ask about.

That is how the SEO/GEO subsystem — 39 authenticated routes — came to have
*zero* tenancy tests. Not because a test failed, but because no test was ever
written, and **an absent test is silent**. The suite was green the whole time.
The same silence hid ``marketing_research_agent/snapshots.py``, which has no
``user_id`` in any of its functions while the MR router around it carefully
passes ``user["id"]`` everywhere else.

So this file does not try to *prove* tenancy — that is undecidable in general,
and the hand-written suites already do the real behavioural work. It does the
one thing they structurally cannot: it makes **omission loud**. Every route the
app serves must appear in :data:`ROUTE_LEDGER` with an explicit classification.
Add a route and forget to think about tenancy, and this file goes red before
review does.

The ledger is also documentation with teeth. ``WORKSPACE_SHARED`` is not a
euphemism for "insecure" — it is a checked-in, counted statement that these
routes serve the same rows to every signed-in caller, which is *correct* for a
single shared team workspace and *wrong* the moment a second client is added.
Today that count is 47. When the workspace boundary lands, the number moves,
and :func:`test_workspace_shared_surface_has_not_grown_silently` makes anyone
who grows it say so on purpose.
"""
from __future__ import annotations

import pytest
from fastapi.routing import APIRoute

from app.main import app as fastapi_app
from app.routers.tests.conftest import client
from app.security import get_current_user, require_admin, require_creator

# --------------------------------------------------------------------------- #
# Classifications
# --------------------------------------------------------------------------- #

#: Reads/writes only the caller's own rows. The per-agent cross-tenant suites
#: are what actually prove these; the ledger only pins that they claim to be.
TENANT_SCOPED = "TENANT_SCOPED"

#: Deliberately serves the same rows to every signed-in caller. Correct for one
#: shared team workspace; a data leak the day a second tenant signs in. Every
#: entry here is a known item on the workspace-tenancy migration, not an
#: oversight.
WORKSPACE_SHARED = "WORKSPACE_SHARED"

#: Static or near-static reference data (brand kits, fonts, element libraries,
#: creative types). Shared on purpose and carries no per-user rows at all.
SHARED_CATALOG = "SHARED_CATALOG"

#: Behind ``require_admin`` / ``require_creator``. Global by design — these are
#: the panels that manage the deployment itself.
ADMIN_ONLY = "ADMIN_ONLY"
CREATOR_ONLY = "CREATOR_ONLY"

#: No FastAPI auth dependency, but guarded *inside the handler* by a shared
#: secret compared with :func:`hmac.compare_digest`. A dependency-graph scan
#: alone would call these unauthenticated, which is why
#: :func:`test_cron_routes_reject_a_missing_or_wrong_secret` asserts the
#: behaviour rather than trusting this label.
CRON_SECRET = "CRON_SECRET"

#: Genuinely public: health, the sign-in exchange, and OAuth callbacks that
#: must be reachable by the provider's redirect.
PUBLIC_BY_DESIGN = "PUBLIC_BY_DESIGN"

#: Classifications that mean "an authenticated caller reaches this".
_AUTHENTICATED = {
    TENANT_SCOPED,
    WORKSPACE_SHARED,
    SHARED_CATALOG,
    ADMIN_ONLY,
    CREATOR_ONLY,
}

# --------------------------------------------------------------------------- #
# The ledger
# --------------------------------------------------------------------------- #

#: Every ``(method, path)`` the app serves, and what it does about tenancy.
#:
#: Keep it sorted by classification then path — that is how it was generated and
#: how a diff stays readable. A new route belongs in the group that describes
#: what it *actually does*, which usually means reading the handler, not the
#: route name.
ROUTE_LEDGER: dict[tuple[str, str], str] = {
    # --- admin / creator panels ------------------------------------------- #
    ("GET", "/api/admin/analytics"): ADMIN_ONLY,
    ("POST", "/api/admin/brands/refresh-packs"): ADMIN_ONLY,
    ("GET", "/api/admin/db/collections"): ADMIN_ONLY,
    ("GET", "/api/admin/db/collections/{name}"): ADMIN_ONLY,
    ("POST", "/api/admin/db/purge-telemetry"): ADMIN_ONLY,
    ("GET", "/api/admin/image-library"): ADMIN_ONLY,
    ("GET", "/api/admin/image-library/{run_id}/image"): ADMIN_ONLY,
    ("GET", "/api/admin/users"): ADMIN_ONLY,
    ("POST", "/api/ref-library/ingest"): ADMIN_ONLY,
    ("POST", "/api/ref-library/sync-drive"): ADMIN_ONLY,
    ("GET", "/api/admin/agents"): CREATOR_ONLY,
    ("POST", "/api/admin/agents/{agent_id}"): CREATOR_ONLY,
    ("GET", "/api/admin/settings"): CREATOR_ONLY,
    ("POST", "/api/admin/settings"): CREATOR_ONLY,
    ("POST", "/api/admin/settings/test"): CREATOR_ONLY,
    ("PUT", "/api/geo/brands/{brand_id}/config"): CREATOR_ONLY,
    ("PUT", "/api/geo/brands/{brand_id}/prompts"): CREATOR_ONLY,
    ("POST", "/api/geo/brands/{brand_id}/prompts/custom"): CREATOR_ONLY,
    ("POST", "/api/geo/brands/{brand_id}/prompts/generate"): CREATOR_ONLY,
    ("POST", "/api/geo/brands/{brand_id}/rescan"): CREATOR_ONLY,
    ("POST", "/api/geo/brands/{brand_id}/strategy/generate"): CREATOR_ONLY,
    ("POST", "/api/news"): CREATOR_ONLY,
    ("POST", "/api/seo-geo/brands"): CREATOR_ONLY,
    ("DELETE", "/api/seo-geo/brands/{brand_id}"): CREATOR_ONLY,
    ("PUT", "/api/seo-geo/competitors/{brand_id}"): CREATOR_ONLY,
    ("POST", "/api/seo-geo/oauth/disconnect/{brand_id}"): CREATOR_ONLY,
    # --- scheduled jobs, guarded by a shared secret in the handler --------- #
    ("POST", "/api/geo/cron/poll"): CRON_SECRET,
    ("POST", "/api/mr/cron/refresh"): CRON_SECRET,
    ("POST", "/api/seo-geo/cron/run"): CRON_SECRET,
    # --- public by design -------------------------------------------------- #
    ("GET", "/"): PUBLIC_BY_DESIGN,
    ("POST", "/api/auth/google"): PUBLIC_BY_DESIGN,
    ("GET", "/api/canva/callback"): PUBLIC_BY_DESIGN,
    ("GET", "/api/health"): PUBLIC_BY_DESIGN,
    ("GET", "/api/seo-geo/oauth/callback"): PUBLIC_BY_DESIGN,
    # --- shared reference data --------------------------------------------- #
    ("GET", "/api/blog/brands"): SHARED_CATALOG,
    ("GET", "/api/blog/brands/{brand_id}/inventory"): SHARED_CATALOG,
    ("POST", "/api/blog/brands/{brand_id}/inventory"): SHARED_CATALOG,
    ("GET", "/api/blog/brands/{brand_id}/voice"): SHARED_CATALOG,
    ("POST", "/api/blog/brands/{brand_id}/voice"): SHARED_CATALOG,
    ("GET", "/api/brands"): SHARED_CATALOG,
    ("GET", "/api/brands/{brand_id}"): SHARED_CATALOG,
    ("GET", "/api/brands/{brand_id}/kit"): SHARED_CATALOG,
    ("GET", "/api/creative/types"): SHARED_CATALOG,
    ("GET", "/api/gd/brands"): SHARED_CATALOG,
    ("GET", "/api/gd/config"): SHARED_CATALOG,
    ("GET", "/api/gd/elements"): SHARED_CATALOG,
    ("GET", "/api/gd/fonts/{font_name}"): SHARED_CATALOG,
    ("GET", "/api/gd/ingested-brands"): SHARED_CATALOG,
    ("GET", "/api/gd/prompts"): SHARED_CATALOG,
    ("GET", "/api/library"): SHARED_CATALOG,
    ("GET", "/api/mr/config"): SHARED_CATALOG,
    ("GET", "/api/mr/connectors"): SHARED_CATALOG,
    ("GET", "/api/news"): SHARED_CATALOG,
    ("GET", "/api/ref-library"): SHARED_CATALOG,
    ("GET", "/api/ref-library/asset/{record_id}"): SHARED_CATALOG,
    ("GET", "/api/ref-library/retrieve"): SHARED_CATALOG,
    ("GET", "/api/ref-library/types"): SHARED_CATALOG,
    # --- scoped to the caller ---------------------------------------------- #
    ("GET", "/api/blog/runs"): TENANT_SCOPED,
    ("POST", "/api/blog/runs"): TENANT_SCOPED,
    ("GET", "/api/blog/runs/{run_id}"): TENANT_SCOPED,
    ("POST", "/api/blog/runs/{run_id}/blocks/{block_id}/comment"): TENANT_SCOPED,
    ("POST", "/api/blog/runs/{run_id}/draft"): TENANT_SCOPED,
    ("GET", "/api/blog/runs/{run_id}/export"): TENANT_SCOPED,
    ("POST", "/api/blog/runs/{run_id}/research/step"): TENANT_SCOPED,
    ("POST", "/api/blog/runs/{run_id}/visuals"): TENANT_SCOPED,
    ("POST", "/api/creative/runs"): TENANT_SCOPED,
    ("GET", "/api/creative/runs/{run_id}"): TENANT_SCOPED,
    ("POST", "/api/creative/runs/{run_id}/acknowledge"): TENANT_SCOPED,
    ("GET", "/api/creative/runs/{run_id}/artifact/{name}"): TENANT_SCOPED,
    ("POST", "/api/creative/runs/{run_id}/autonomous"): TENANT_SCOPED,
    ("GET", "/api/creative/runs/{run_id}/decisions"): TENANT_SCOPED,
    ("POST", "/api/creative/runs/{run_id}/generate"): TENANT_SCOPED,
    ("POST", "/api/creative/runs/{run_id}/intent"): TENANT_SCOPED,
    ("POST", "/api/creative/runs/{run_id}/override"): TENANT_SCOPED,
    ("POST", "/api/creative/runs/{run_id}/plan"): TENANT_SCOPED,
    ("POST", "/api/creative/runs/{run_id}/plan/approve"): TENANT_SCOPED,
    ("POST", "/api/creative/runs/{run_id}/plan/text"): TENANT_SCOPED,
    ("POST", "/api/gd/runs"): TENANT_SCOPED,
    ("GET", "/api/gd/runs/{run_id}"): TENANT_SCOPED,
    ("POST", "/api/gd/runs/{run_id}/approve"): TENANT_SCOPED,
    ("GET", "/api/gd/runs/{run_id}/artifact/{rel:path}"): TENANT_SCOPED,
    ("POST", "/api/gd/runs/{run_id}/back"): TENANT_SCOPED,
    ("GET", "/api/gd/runs/{run_id}/brand-logo"): TENANT_SCOPED,
    ("GET", "/api/gd/runs/{run_id}/brand-logos"): TENANT_SCOPED,
    ("POST", "/api/gd/runs/{run_id}/config"): TENANT_SCOPED,
    ("POST", "/api/gd/runs/{run_id}/elements/upload"): TENANT_SCOPED,
    ("POST", "/api/gd/runs/{run_id}/generate"): TENANT_SCOPED,
    ("POST", "/api/gd/runs/{run_id}/plan"): TENANT_SCOPED,
    ("GET", "/api/gd/runs/{run_id}/prompt"): TENANT_SCOPED,
    ("POST", "/api/gd/runs/{run_id}/stage4"): TENANT_SCOPED,
    ("POST", "/api/gd/runs/{run_id}/subject/upload"): TENANT_SCOPED,
    ("POST", "/api/gd/runs/{run_id}/suggest"): TENANT_SCOPED,
    ("POST", "/api/gd/runs/{run_id}/suggest-placement"): TENANT_SCOPED,
    ("POST", "/api/gd/runs/{run_id}/text-preview"): TENANT_SCOPED,
    ("POST", "/api/gd/runs/{run_id}/tweak"): TENANT_SCOPED,
    ("POST", "/api/mr/ask"): TENANT_SCOPED,
    ("GET", "/api/mr/datasets"): TENANT_SCOPED,
    ("DELETE", "/api/mr/datasets/{dataset_id}"): TENANT_SCOPED,
    ("POST", "/api/mr/ingest"): TENANT_SCOPED,
    ("POST", "/api/mr/ingest-pdf"): TENANT_SCOPED,
    ("POST", "/api/mr/ingest-sheet"): TENANT_SCOPED,
    ("GET", "/api/mr/lead-analysis"): TENANT_SCOPED,
    ("GET", "/api/mr/lead-analysis/pdf"): TENANT_SCOPED,
    ("GET", "/api/mr/overview"): TENANT_SCOPED,
    ("GET", "/api/mr/report-periods"): TENANT_SCOPED,
    ("POST", "/api/mr/reports/{kind}"): TENANT_SCOPED,
    ("GET", "/api/mr/runs"): TENANT_SCOPED,
    ("GET", "/api/mr/runs/{run_id}"): TENANT_SCOPED,
    ("GET", "/api/mr/runs/{run_id}/pdf"): TENANT_SCOPED,
    ("POST", "/api/mr/schedule/{period}"): TENANT_SCOPED,
    ("GET", "/api/mr/sources"): TENANT_SCOPED,
    ("POST", "/api/mr/sources"): TENANT_SCOPED,
    ("DELETE", "/api/mr/sources/{spreadsheet_id}"): TENANT_SCOPED,
    ("GET", "/api/mr/targets"): TENANT_SCOPED,
    ("POST", "/api/mr/targets"): TENANT_SCOPED,
    ("GET", "/api/mr/trends"): TENANT_SCOPED,
    ("GET", "/api/mr/workbook"): TENANT_SCOPED,
    ("POST", "/api/mr/workbook/scan"): TENANT_SCOPED,
    ("GET", "/api/usage"): TENANT_SCOPED,
    # --- shared across the whole workspace, with no boundary object -------- #
    # Canva: one module-level ``_active_token`` in ``routers/canva.py`` holds
    # the most recent OAuth grant, so every caller imports into whichever
    # account authorised last. The file says so itself.
    ("GET", "/api/canva/authorize"): WORKSPACE_SHARED,
    ("POST", "/api/canva/import"): WORKSPACE_SHARED,
    # GEO: every doc id is ``…-{brand_id}``; no user or workspace key exists.
    ("GET", "/api/geo/brands"): WORKSPACE_SHARED,
    ("GET", "/api/geo/brands/{brand_id}/answers"): WORKSPACE_SHARED,
    ("GET", "/api/geo/brands/{brand_id}/comparison"): WORKSPACE_SHARED,
    ("GET", "/api/geo/brands/{brand_id}/config"): WORKSPACE_SHARED,
    ("GET", "/api/geo/brands/{brand_id}/history"): WORKSPACE_SHARED,
    ("GET", "/api/geo/brands/{brand_id}/poll/status"): WORKSPACE_SHARED,
    ("POST", "/api/geo/brands/{brand_id}/poll/step"): WORKSPACE_SHARED,
    ("GET", "/api/geo/brands/{brand_id}/prompts"): WORKSPACE_SHARED,
    ("GET", "/api/geo/brands/{brand_id}/report"): WORKSPACE_SHARED,
    ("GET", "/api/geo/brands/{brand_id}/strategy"): WORKSPACE_SHARED,
    ("PUT", "/api/geo/brands/{brand_id}/strategy/actions/{action_id}"): WORKSPACE_SHARED,
    ("GET", "/api/geo/config"): WORKSPACE_SHARED,
    ("GET", "/api/geo/optimizer/analyses"): WORKSPACE_SHARED,
    ("GET", "/api/geo/optimizer/analyses/{analysis_id}"): WORKSPACE_SHARED,
    ("POST", "/api/geo/optimizer/analyze"): WORKSPACE_SHARED,
    ("POST", "/api/geo/optimizer/rescore"): WORKSPACE_SHARED,
    # MR snapshots: ``marketing_research_agent/snapshots.py`` has no ``user_id``
    # in any function — every row is keyed by vendor slug and date alone, while
    # the rest of the MR router scopes carefully. This is the inconsistency the
    # module docstring describes.
    ("GET", "/api/mr/snapshots"): WORKSPACE_SHARED,
    ("POST", "/api/mr/snapshots/capture"): WORKSPACE_SHARED,
    ("GET", "/api/mr/snapshots/deltas"): WORKSPACE_SHARED,
    ("GET", "/api/mr/snapshots/portfolio"): WORKSPACE_SHARED,
    ("GET", "/api/mr/snapshots/vendor/{slug}"): WORKSPACE_SHARED,
    ("GET", "/api/mr/snapshots/vendor/{slug}/pdf"): WORKSPACE_SHARED,
    # SEO: ``state.save("brands", …)`` is a single global Firestore document,
    # and every other doc id is ``…-{brand_id}``.
    ("POST", "/api/seo-geo/ask/{brand_id}"): WORKSPACE_SHARED,
    ("GET", "/api/seo-geo/audit/{brand_id}"): WORKSPACE_SHARED,
    ("POST", "/api/seo-geo/audit/{brand_id}/run"): WORKSPACE_SHARED,
    ("GET", "/api/seo-geo/brands/{brand_id}"): WORKSPACE_SHARED,
    ("GET", "/api/seo-geo/briefs/{brand_id}"): WORKSPACE_SHARED,
    ("POST", "/api/seo-geo/briefs/{brand_id}"): WORKSPACE_SHARED,
    ("GET", "/api/seo-geo/competitors/{brand_id}"): WORKSPACE_SHARED,
    ("GET", "/api/seo-geo/competitors/{brand_id}/profiles"): WORKSPACE_SHARED,
    ("POST", "/api/seo-geo/competitors/{brand_id}/profiles/refresh"): WORKSPACE_SHARED,
    ("POST", "/api/seo-geo/competitors/{brand_id}/track"): WORKSPACE_SHARED,
    ("POST", "/api/seo-geo/draft-score/{brand_id}"): WORKSPACE_SHARED,
    ("GET", "/api/seo-geo/keywords/{brand_id}"): WORKSPACE_SHARED,
    ("POST", "/api/seo-geo/keywords/{brand_id}/run"): WORKSPACE_SHARED,
    ("GET", "/api/seo-geo/oauth/start/{brand_id}"): WORKSPACE_SHARED,
    ("GET", "/api/seo-geo/overview"): WORKSPACE_SHARED,
    ("GET", "/api/seo-geo/pages/{brand_id}"): WORKSPACE_SHARED,
    ("POST", "/api/seo-geo/pages/{brand_id}/refresh"): WORKSPACE_SHARED,
    ("POST", "/api/seo-geo/run/{brand_id}"): WORKSPACE_SHARED,
    ("POST", "/api/seo-geo/serp/{brand_id}"): WORKSPACE_SHARED,
    ("GET", "/api/seo-geo/site-review/{brand_id}"): WORKSPACE_SHARED,
    ("POST", "/api/seo-geo/site-review/{brand_id}"): WORKSPACE_SHARED,
    ("POST", "/api/seo-geo/todos/{brand_id}/{todo_id}"): WORKSPACE_SHARED,
    ("POST", "/api/seo-geo/update-plan/{brand_id}"): WORKSPACE_SHARED,
}

#: Size of the un-siloed surface at the time this file was written. This is a
#: *ratchet*, not a target: the workspace-tenancy migration should drive it
#: down, and nothing should drive it up without editing this number and saying
#: why in the commit.
WORKSPACE_SHARED_BASELINE = 47


# --------------------------------------------------------------------------- #
# Route discovery
# --------------------------------------------------------------------------- #

def _live_routes() -> set[tuple[str, str]]:
    """Every ``(method, path)`` the app actually serves.

    ``HEAD``/``OPTIONS`` are dropped: Starlette synthesises them and they carry
    no handler of their own, so classifying them would be noise.
    """
    out: set[tuple[str, str]] = set()
    for route in fastapi_app.routes:
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods - {"HEAD", "OPTIONS"}:
            out.add((method, route.path))
    return out


_AUTH_GUARDS = {
    get_current_user: "user",
    require_admin: "admin",
    require_creator: "creator",
}


def _guards_of(route: APIRoute) -> set[str]:
    """Which auth dependencies this route resolves through.

    Walks the whole dependency tree rather than the top level: ``require_admin``
    and ``require_creator`` both depend on ``get_current_user``, and a router
    may attach a guard at include time rather than per endpoint.
    """
    found: set[str] = set()
    seen: set[int] = set()

    def walk(dep) -> None:
        if dep is None or id(dep) in seen:
            return
        seen.add(id(dep))
        if getattr(dep, "call", None) in _AUTH_GUARDS:
            found.add(_AUTH_GUARDS[dep.call])
        for sub in getattr(dep, "dependencies", []) or []:
            walk(sub)

    walk(route.dependant)
    return found


# --------------------------------------------------------------------------- #
# The gate
# --------------------------------------------------------------------------- #

def test_every_route_is_classified() -> None:
    """A route the ledger does not mention fails here — the whole point.

    This is the test that would have caught SEO/GEO in June: not by proving the
    endpoints were unscoped, but by refusing to let 39 of them exist without
    anybody writing down what they do.
    """
    missing = sorted(_live_routes() - set(ROUTE_LEDGER))
    assert not missing, (
        "These routes are not in ROUTE_LEDGER. Read the handler, decide what it "
        "does about tenancy, and add it to the right group in "
        "app/routers/tests/test_route_tenancy_conformance.py:\n  "
        + "\n  ".join(f"{m} {p}" for m, p in missing)
    )


def test_ledger_has_no_stale_entries() -> None:
    """A deleted route must leave the ledger, or the counts below start lying."""
    stale = sorted(set(ROUTE_LEDGER) - _live_routes())
    assert not stale, (
        "These ledger entries no longer match a live route — delete them:\n  "
        + "\n  ".join(f"{m} {p}" for m, p in stale)
    )


def test_classifications_match_the_real_dependency_graph() -> None:
    """The label must agree with the guard the route actually sits behind.

    Catches the reverse mistake from :func:`test_every_route_is_classified`: a
    route that *is* in the ledger, but whose guard was later loosened —
    ``require_creator`` swapped for ``get_current_user`` in a hurry — while the
    label kept saying CREATOR_ONLY.
    """
    wrong: list[str] = []
    for route in fastapi_app.routes:
        if not isinstance(route, APIRoute):
            continue
        guards = _guards_of(route)
        for method in route.methods - {"HEAD", "OPTIONS"}:
            label = ROUTE_LEDGER.get((method, route.path))
            if label is None:
                continue  # reported by test_every_route_is_classified
            if label in (CRON_SECRET, PUBLIC_BY_DESIGN):
                expected_empty = True
            else:
                expected_empty = False
            if expected_empty and guards:
                wrong.append(f"{method} {route.path}: {label} but has guards {sorted(guards)}")
            elif not expected_empty and not guards:
                wrong.append(f"{method} {route.path}: {label} but has NO auth dependency")
            elif label == ADMIN_ONLY and "admin" not in guards:
                wrong.append(f"{method} {route.path}: ADMIN_ONLY but no require_admin")
            elif label == CREATOR_ONLY and "creator" not in guards:
                wrong.append(f"{method} {route.path}: CREATOR_ONLY but no require_creator")
    assert not wrong, "Ledger disagrees with the dependency graph:\n  " + "\n  ".join(wrong)


def test_workspace_shared_surface_has_not_grown_silently() -> None:
    """Ratchet on the un-siloed surface.

    Shrinking this is the migration's job and needs the baseline lowered in the
    same commit. Growing it is occasionally legitimate — and must be a sentence
    somebody wrote on purpose, not a diff nobody noticed.
    """
    actual = sum(1 for label in ROUTE_LEDGER.values() if label == WORKSPACE_SHARED)
    assert actual <= WORKSPACE_SHARED_BASELINE, (
        f"WORKSPACE_SHARED grew from {WORKSPACE_SHARED_BASELINE} to {actual}. "
        "If that is deliberate, raise WORKSPACE_SHARED_BASELINE and say why."
    )
    assert actual == WORKSPACE_SHARED_BASELINE, (
        f"WORKSPACE_SHARED is down to {actual} from {WORKSPACE_SHARED_BASELINE} — "
        "nice. Lower WORKSPACE_SHARED_BASELINE to lock the win in."
    )


@pytest.mark.parametrize(
    ("method", "path"),
    sorted(mp for mp, label in ROUTE_LEDGER.items() if label in _AUTHENTICATED),
)
def test_authenticated_routes_refuse_an_anonymous_caller(
    method: str, path: str, unauthenticated
) -> None:
    """Every non-public route must answer 401/403 with no bearer token.

    Runs against the real ``get_current_user`` — ``unauthenticated`` drops the
    override — so this exercises the actual guard, not a stub. Path parameters
    are filled with a nonsense value on purpose: a route that rejects the caller
    before parsing them is behaving correctly, and one that 404s on the id
    *before* checking the token would show up here as a missing 401.
    """
    unauthenticated()
    url = path
    while "{" in url:
        head, _, rest = url.partition("{")
        _, _, tail = rest.partition("}")
        url = f"{head}conformance-probe{tail}"
    resp = client.request(method, url)
    assert resp.status_code in (401, 403), (
        f"{method} {url} answered {resp.status_code} to an anonymous caller; "
        "expected 401 or 403."
    )


@pytest.mark.parametrize(
    ("method", "path"),
    sorted(mp for mp, label in ROUTE_LEDGER.items() if label == CRON_SECRET),
)
def test_cron_routes_reject_a_missing_or_wrong_secret(method: str, path: str) -> None:
    """The cron endpoints carry no FastAPI dependency — assert the real guard.

    Labelling them CRON_SECRET is a claim about code inside the handler. This is
    the test that makes the claim answerable: no header, and a wrong header,
    must both be refused. 503 counts as refusal — that is what the handlers
    answer when the secret is not configured at all, which is still "you did not
    get in".
    """
    for headers in ({}, {"x-cron-key": "not-the-key"}):
        resp = client.request(method, path, headers=headers)
        assert resp.status_code in (401, 403, 503), (
            f"{method} {path} answered {resp.status_code} with headers={headers}; "
            "a cron endpoint must refuse an unauthenticated caller."
        )
