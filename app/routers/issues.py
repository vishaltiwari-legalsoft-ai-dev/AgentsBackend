"""The Issues record — everything the console already knows is wrong, in one read.

Mounted under ``/api``. Any signed-in caller reads it; it composes the shared
brand registry exactly as ``/seo-geo/overview`` does, so it is WORKSPACE_SHARED
in the tenancy ledger for the same reason.

One rule governs the handler: **a source that cannot be read is an issue, not
a 500 and not an empty section.** Each read is wrapped on its own, the failure
goes to the log with its traceback, and a low-severity "could not be read"
issue takes its place — so a Firestore blip or a module that has not shipped
yet can never render as "all clear". The builders in
``app.services.issues`` decide what the signals mean; this file only decides
where they come from.
"""
from __future__ import annotations

import datetime as dt
import importlib
import logging
from typing import Any, Callable

from fastapi import APIRouter, Depends

from app.security import get_current_user
from app.services import issues as issues_svc
from final_geo_agent import geo_engines, geo_poll, geo_strategy
from seo_geo_agent import insights
from seo_geo_agent import state as seo_state

router = APIRouter()
logger = logging.getLogger("agentos.issues")

#: The run-log module lands from another package; it is resolved by name at
#: request time so this router imports (and the app boots) before it exists,
#: and its absence reads as an unreadable source rather than a healthy one.
GEO_RUNLOG_MODULE = "final_geo_agent.geo_runlog"

#: Sentinel for "this source could not be read" — distinct from a source that
#: read fine and had nothing (``None``).
_UNREAD = object()


def _recent_runs(brand_id: str, n: int) -> list[dict]:
    module = importlib.import_module(GEO_RUNLOG_MODULE)
    return module.recent_runs(brand_id, n)


def _read(
    sink: list[dict], area: str, brand: dict, source: str, fn: Callable[[], Any],
) -> Any:
    """Run one read; on failure log it and leave an issue in its place."""
    try:
        return fn()
    except Exception:  # noqa: BLE001 — every source degrades to an issue
        logger.exception("issues: %s could not be read for brand %r", source, brand.get("id"))
        sink.append(issues_svc.unreadable_issue(area, brand, source))
        return _UNREAD


def _brand_issues(brand: dict, engine_status: dict | None) -> list[dict]:
    found: list[dict] = []
    brand_id = brand["id"]

    run = _read(found, "seo", brand, "SEO run", lambda: seo_state.load(f"run-{brand_id}"))
    if run is not _UNREAD:
        found.extend(issues_svc.issues_from_seo(brand, run))

    cfg = _read(found, "geo", brand, "GEO configuration", lambda: geo_poll.ensure_config(brand))
    runs = _read(found, "geo", brand, "GEO run log", lambda: _recent_runs(brand_id, 1))
    plan = _read(found, "geo", brand, "GEO plan", lambda: geo_strategy.load_strategy(brand_id))
    found.extend(issues_svc.issues_from_geo(
        brand,
        None if cfg is _UNREAD else cfg,
        engine_status,
        (runs[0] if runs else None) if runs is not _UNREAD and isinstance(runs, list) else None,
        plan=None if plan is _UNREAD else plan,
    ))
    return found


@router.get("/issues")
def list_issues(user: dict = Depends(get_current_user)) -> dict:
    """Every open issue across every enabled brand, most severe first."""
    found: list[dict] = []
    workspace = issues_svc.WORKSPACE_BRAND

    brands = _read(found, "seo", workspace, "Brand registry", insights.list_brands)
    engine_status = _read(found, "geo", workspace, "Engine status", geo_engines.engine_status)
    if engine_status is _UNREAD:
        engine_status = None

    if brands is not _UNREAD:
        for brand in brands:
            if brand.get("enabled", True):
                found.extend(_brand_issues(brand, engine_status))

    body = issues_svc.build_issues(found)
    body["generated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    return body
