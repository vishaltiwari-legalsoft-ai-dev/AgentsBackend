"""Marketing Research agent API (spec §3–§4). Endpoints under ``/api/mr``.

Data enters via CSV/Excel export upload (``/mr/ingest``); the live Google Ads /
META / HubSpot connectors share the same ``DataSource`` interface and slot in
when credentials are provisioned. Reports and ingested datasets are persisted as
runs, owned by the authenticated user.
"""

from __future__ import annotations

import hmac
import io
import logging
import math
import os
import tempfile
import threading
import time
from datetime import date, datetime, timezone

import httpx
from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile,
)
from fastapi.responses import StreamingResponse

from app.security import get_current_user
from app.services.run_tracking import (
    CHANGE, CRON, JOB, Activity, ActivityTrail, silent,
)

from dataclasses import asdict

from marketing_research_agent import config as mr_config
from marketing_research_agent import insight as mr_insight
from marketing_research_agent import lead_analysis as mr_leads
from marketing_research_agent import pdf_export as mr_pdf
from marketing_research_agent import profiles as mr_profiles
from marketing_research_agent import reports, runs, schedule
from marketing_research_agent import snapshots as mr_snapshots
from marketing_research_agent import sources_registry as mr_sources_registry
from marketing_research_agent import trends as mr_trends
from marketing_research_agent import workbook as mr_workbook
from marketing_research_agent import workspace as mr_workspace
from marketing_research_agent.config import COLUMN_MAPS
from marketing_research_agent.schemas import CampaignMetric, DateRange, Lead
from marketing_research_agent.sources.csv_source import CsvSource
from marketing_research_agent.sources.sheets_source import (
    SheetsSource,
    fetch_all_trackers,
    fetch_official_totals,
    fetch_tab_values,
    is_rollup_platform,
    is_rollup_tab,
    parse_tracker,
    reconcile_official_spend,
    workbook_meta,
)

router = APIRouter()
logger = logging.getLogger("agentos.mr")

MR_AGENT_ID = "a6"  # "Market Researcher" slot in the frontend agent catalog
MR_AGENT_NAME = "Marketing Research"
_FULL_RANGE = DateRange(start=date(2000, 1, 1), end=date(2100, 1, 1))


# ---------------------- sheet-pull failure contract ----------------------
# ``mr_runs`` is the ONLY copy of parsed tracker state — there is no restore
# path — so the pull must never destroy before it has a replacement in hand,
# and a pull that destroyed nothing but produced nothing must not answer 200.

class SheetPullError(RuntimeError):
    """The pull could not produce new data. Nothing was swapped; the previous
    runs are intact."""


class SheetPullBusy(RuntimeError):
    """A pull for this workspace is already in flight in this process."""


class SheetPullTooSoon(RuntimeError):
    """A FORCED pull arrived inside the floor no forced pull may cross.
    ``retry_after`` is whole seconds until it clears (the ``Retry-After``)."""

    def __init__(self, message: str, retry_after: int) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class SheetTabRefused(RuntimeError):
    """A single-tab pull was refused BEFORE anything was written: the tab does
    not exist, is the roll-up, or does not parse as a tracker. ``status`` is the
    HTTP status the caller gets (404 / 422)."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


# 207 (RFC 4918 Multi-Status) = some of it worked. Deliberately still 2xx: a
# partially-degraded pull must not make Cloud Scheduler retry a permanently bad
# tab forever, so it stays 2xx and shouts in the log instead. Total failure is a
# 5xx (below), which the scheduler does retry and alert on.
#
# "fresh" is a pull that was NOT run because this workspace was pulled moments
# ago (see ``_pull_gate``). It is 200 because nothing failed and nothing is
# stale — and it is a distinct status, with the time of the pull it is pointing
# at, so it can never be mistaken for a pull that fetched something. (A FORCED
# pull inside the floor is not "fresh": that is a 429, a real refusal.)
_PULL_HTTP_STATUS = {"ok": 200, "partial": 207, "fresh": 200}

# Per-workspace overlap guard. Two pulls interleaving their write and delete
# passes is a data-loss race; this is a single-process guard (Cloud Run runs
# several instances), which closes the common case — cron firing while the user
# hits "Pull" — but is not a distributed lease.
_PULL_LOCKS: dict[str, threading.Lock] = {}
_PULL_LOCKS_GUARD = threading.Lock()


def _pull_lock(user_id: str) -> threading.Lock:
    with _PULL_LOCKS_GUARD:
        return _PULL_LOCKS.setdefault(user_id, threading.Lock())


def _ws(user: dict) -> str:
    """The key this caller's WORKBOOK-DERIVED runs live under.

    The ONLY place that key is chosen — see ``marketing_research_agent.workspace``
    for the rule and the opt-in switch (sharing is OFF unless
    ``MR_WORKSPACE_SHARED`` is explicitly on, so this is the caller's own id
    everywhere it has not been deliberately enabled). It is resolved from the
    server's own configuration, never from the request, so no caller can name
    another key.

    Use it for the three kinds a sheet pull produces (``dataset``,
    ``official_spend``, ``lead_analysis``), for uploads that join that
    dashboard, and for the board report. Do NOT use it for anything a person
    builds for themselves — ``/mr/reports/{kind}``, ``/mr/runs*``,
    ``/mr/schedule/*`` and targets stay keyed on ``user["id"]``.

    The helpers below (``_load_dataset``, ``_latest_*``, ``_pull_and_swap``)
    keep taking an explicit ``user_id`` on purpose: they receive whichever key
    the route resolved, and none of them decides one for itself.
    """
    return mr_workspace.workspace_id(user["id"])


def _is_workspace_admin(user: dict) -> bool:
    """Admin or creator — the only callers who may change what a whole
    workspace sees while the workbook data is shared. The same two flags
    ``/mr/sources`` uses (``_may_remove_any_sheet``)."""
    return bool(user.get("is_admin") or user.get("is_creator"))


def _require_admin_while_shared(user: dict, message: str) -> None:
    """403 with ``message`` when the workspace is shared and the caller is not an
    admin/creator. A no-op unshared: the workspace is then the caller's own, so
    whatever they write, only they read, and today's behaviour is untouched.

    Fails closed on the CALLER — the check reads roles from the authenticated
    user, never from the request body."""
    if mr_workspace.is_shared() and not _is_workspace_admin(user):
        raise HTTPException(403, message)


#: Every unit of Marketing Research work lands here — see THE RULE in run_tracking.py.
trail = ActivityTrail(agent_id=MR_AGENT_ID, agent_name=MR_AGENT_NAME, category="data")


def _save_csv_tmp(content: bytes) -> str:
    fd = tempfile.NamedTemporaryFile(delete=False, suffix=".csv")
    fd.write(content)
    fd.close()
    return fd.name


def _rehydrate_metrics(rows: list[dict]) -> list[CampaignMetric]:
    out = []
    for r in rows:
        r = dict(r)
        r["date"] = date.fromisoformat(str(r["date"])[:10])
        out.append(CampaignMetric(**r))
    return out


def _rehydrate_leads(rows: list[dict]) -> list[Lead]:
    out = []
    for r in rows:
        r = dict(r)
        r["created_at"] = date.fromisoformat(str(r["created_at"])[:10])
        out.append(Lead(**r))
    return out


def _latest_datasets(user_id: str, all_runs: list[dict] | None = None) -> dict[str, dict]:
    """Newest dataset run per ``platform`` so a re-pull supersedes the prior
    copy rather than double-counting it.

    ``all_runs`` is a run list the caller has already fetched — pass it and this
    costs nothing (see :func:`_load_dataset`); omit it and it runs its own
    ``kind="dataset"`` query."""
    latest: dict[str, dict] = {}
    for run in (runs.list_runs(user_id, kind="dataset") if all_runs is None else all_runs):
        if run.get("kind") != "dataset":
            continue
        plat = run.get("platform", run["id"])
        # Stale-data guard: a rollup tab ingested before the fetch-time skip
        # existed would double-count every vendor dollar forever.
        if is_rollup_platform(plat):
            continue
        prev = latest.get(plat)
        if prev is None or run.get("generated_at", "") > prev.get("generated_at", ""):
            latest[plat] = run
    return latest


def _vendor_label(platform: str) -> str:
    """Human vendor name from a dataset's platform key ("sheets:<tab>" → tab)."""
    plat = str(platform or "")
    for prefix in ("sheets:", "pdf:"):
        if plat.startswith(prefix):
            return plat[len(prefix):]
    return plat


def _latest_official_run(user_id: str, all_runs: list[dict] | None = None) -> dict:
    """Newest official-totals pull from the sheet's Overall tab; {} if never
    pulled. Carries both the full per-field map ("totals") and the legacy
    spend-only map ("months")."""
    newest: dict | None = None
    for run in (runs.list_runs(user_id, kind="official_spend")
                if all_runs is None else all_runs):
        if run.get("kind") != "official_spend":
            continue
        if newest is None or run.get("generated_at", "") > newest.get("generated_at", ""):
            newest = run
    return newest or {}


def _latest_official_spend(user_id: str) -> dict[str, float]:
    """Spend-only view ({"YYYY-MM": spend}) — the trends board's headline."""
    return dict(_latest_official_run(user_id).get("months") or {})


def _latest_lead_run(user_id: str, all_runs: list[dict] | None = None) -> dict | None:
    """Newest persisted lead-analysis summary run; None if never captured."""
    newest: dict | None = None
    for run in (runs.list_runs(user_id, kind="lead_analysis")
                if all_runs is None else all_runs):
        if run.get("kind") != "lead_analysis":
            continue
        if newest is None or run.get("generated_at", "") > newest.get("generated_at", ""):
            newest = run
    return newest


def _tracker_rollups_by_month(user_id: str) -> dict[str, dict[str, dict]]:
    """Tracker funnel counts per month per vendor slug — the join the lead
    sheet's QL-ratio/booking-rate rule needs (the lead sheet has no lead totals,
    only booked-demo rows)."""
    out: dict[str, dict[str, dict]] = {}
    for plat, run in _latest_datasets(user_id).items():
        vendor = _vendor_label(plat)
        slug = mr_snapshots.slugify(vendor)
        for m in _rehydrate_metrics(run.get("metrics", [])):
            ym = f"{m.date.year:04d}-{m.date.month:02d}"
            r = out.setdefault(ym, {}).setdefault(
                slug, {"vendor": vendor, "leads": 0, "qualified_leads": 0, "demos_booked": 0})
            r["leads"] += m.leads
            r["qualified_leads"] += m.qualified_leads
            r["demos_booked"] += m.demos_booked
    return out


def _build_lead_analysis(user_id: str, year: int) -> tuple[dict, dict] | None:
    """Find the lead-analysis tab across every connected workbook (auto-detected
    by its header row — nothing to configure) and aggregate it into a run.

    Builds only — the caller persists, so a failure here can never cost the
    previous summary. Returns ``(run, result_row)``, or ``None`` when every
    workbook was read cleanly and none of them holds a lead tab.

    Raises ``SheetPullError`` when a workbook could not be read at all. An
    unreadable sheet must never be mistaken for "this workspace has no lead
    tab": that read is what decides whether the previous summary gets retired.
    """
    workbooks = [{"id": mr_config.SHEETS_SPREADSHEET_ID, "label": "Primary marketing tracker"}]
    workbooks += [{"id": s["id"], "label": str(s.get("label") or s["id"][:8])}
                  for s in mr_sources_registry.extra_sources()]
    found: tuple[dict, str] | None = None
    unreadable: list[str] = []
    for wb in workbooks:
        try:
            grids = mr_workbook.fetch_workbook(wb["id"], max_rows=20)  # header scan only
        except Exception as exc:
            unreadable.append(f"{wb['label']} ({exc})")
            continue  # an unreadable secondary never blocks the others
        for g in grids:
            if mr_leads.find_lead_tab(g.rows):
                found = (wb, g.title)
                break
        if found:
            break
    if not found:
        if unreadable:
            raise SheetPullError("could not read " + "; ".join(unreadable))
        return None
    wb, tab = found
    rows = fetch_tab_values(wb["id"], tab)  # full tab — lead sheets outgrow the grid cap
    records, gaps = mr_leads.parse_lead_rows(rows, year=year)
    # The lead-quality flags are frozen into the run, so they must be judged
    # against the thresholds of the workspace this run is being written for.
    # ``user_id`` here is the WORKSPACE key — ``_ws(user)`` from the UI, the same
    # resolved key from the cron — so with the shared workspace the summary every
    # member reads is flagged against ``targets__{workspace}`` (the owner's red
    # lines), not against whichever member pressed Pull. A member's private
    # targets therefore never leak into, or get silently overridden by, a
    # summary the whole workspace shares; and the cron stays just another
    # caller, never a special case that judges against somebody else's line.
    from marketing_research_agent import goals as mr_goals

    summary = mr_leads.summarize(
        records,
        tracker_rollups=_tracker_rollups_by_month(user_id),
        thresholds=mr_goals.thresholds(mr_goals.get_targets(user_id)),
    )
    run = {
        "id": runs.new_run_id(), "kind": "lead_analysis", "user_id": user_id,
        "agent_id": MR_AGENT_ID, "platform": "sheets-leads",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_label": wb["label"], "tab": tab, "gaps": gaps,
        "summary": summary,
    }
    flags = sum(b.get("flag_count", 0) for b in summary["months"].values())
    return run, {"tab": f"Lead analysis ({tab})", "rows": len(records),
                 "months": len(summary["months"]), "lead_flags": flags}


# The three kinds ``_load_dataset`` reassembles. Naming them keeps the report
# runs — the only kinds that grow without bound — out of the query entirely.
_DATASET_KINDS = ("dataset", "official_spend", "lead_analysis")


def _load_dataset(user_id: str) -> dict:
    """Reassemble one workspace's ingested data into one dataset. Keeps a
    per-vendor view (one entry per source tab/upload) so reports can name
    vendors. ``user_id`` is the key the caller resolved — ``_ws(user)`` for the
    shared workbook data, which is what every route but the private ones passes.

    ONE run query serves all three components. This used to call ``list_runs``
    three times — and each of those was a full unfiltered scan of ``mr_runs``,
    so ``/mr/overview`` alone read every other workspace's runs three times over.
    """
    all_runs = runs.list_runs(user_id, kind=_DATASET_KINDS)
    latest = _latest_datasets(user_id, all_runs)
    metrics: list[CampaignMetric] = []
    leads: list[Lead] = []
    vendor_metrics: dict[str, list[CampaignMetric]] = {}
    for plat, run in sorted(latest.items()):
        ms = _rehydrate_metrics(run.get("metrics", []))
        metrics.extend(ms)
        leads.extend(_rehydrate_leads(run.get("leads", [])))
        if ms:
            vendor_metrics.setdefault(_vendor_label(plat), []).extend(ms)
    sources = [
        {"platform": plat, "generated_at": run.get("generated_at"),
         "metrics": len(run.get("metrics", [])), "leads": len(run.get("leads", []))}
        for plat, run in sorted(latest.items())
    ]
    official = _latest_official_run(user_id, all_runs)
    lead_run = _latest_lead_run(user_id, all_runs)
    return {"metrics": metrics, "leads": leads, "vendor_metrics": vendor_metrics,
            "official_spend": dict(official.get("months") or {}),
            "official_totals": dict(official.get("totals") or {}),
            # WHEN those official figures were pulled. The board report keys its
            # idempotency on it, so a fresh pull re-derives instead of serving
            # the roll-up of a capture that has since been replaced.
            "official_captured_at": official.get("generated_at"),
            "lead_summary": (lead_run or {}).get("summary"),
            "today": date.today(), "sources": sources}


#: The 403s a member gets, while the workbook data is shared, for the three
#: things that change what the WHOLE team's dashboard shows: an upload, a forced
#: pull and a single-tab pull. Plain language, and each says who CAN. Pinned
#: verbatim in the tests so the wording the console shows cannot drift from the
#: wording raised here.
_UPLOAD_REFUSED = "Uploads go to the whole team's dashboard, so only an admin can add them."
_FORCE_REFUSED = ("Only an admin can force a pull. The team's data refreshes on its own "
                  "and a normal pull is always available.")
_SINGLE_TAB_REFUSED = ("Pulling a single tab changes the whole team's dashboard, so only an "
                       "admin can do it. A normal pull refreshes every tab.")


@router.post("/mr/ingest")
async def ingest(
    file: UploadFile = File(...),
    platform: str = Form(...),
    user=Depends(get_current_user),
    act: Activity = trail.records("ingest", "Uploaded a platform export"),
):
    """Upload a platform export (CSV) as a dataset.

    While the workbook data is shared an upload joins the ONE dashboard the whole
    team reads, replaces that platform's figure for everyone (the newest run per
    platform wins) and is permanent (``dataset`` is exempt from retention) — so
    only an admin/creator may add one (403 otherwise). Checked FIRST, before the
    file is read or the platform validated: a refusal must not depend on, or
    teach anything about, the rest of the request. Unshared, the upload is the
    caller's own and this is what it always was."""
    _require_admin_while_shared(user, _UPLOAD_REFUSED)
    if platform not in COLUMN_MAPS:
        raise HTTPException(400, f"unknown platform '{platform}' (expected one of {list(COLUMN_MAPS)})")
    content = await file.read()
    path = _save_csv_tmp(content)
    src = CsvSource(path, platform=platform)

    metrics, m_gaps = [], []
    leads, l_gaps = [], []
    if platform == "hubspot":
        leads, l_gaps = src.fetch_leads(_FULL_RANGE)
    else:
        metrics, m_gaps = src.fetch_campaign_metrics(_FULL_RANGE)

    run = {
        "id": runs.new_run_id(),
        "kind": "dataset",
        # The WORKSPACE key: an upload joins the dashboard everyone reads. Who
        # added it is a separate fact, and it is what decides who may delete it.
        "user_id": _ws(user),
        "created_by": user["id"],
        "agent_id": MR_AGENT_ID,
        "platform": platform,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "metrics": [m.__dict__ for m in metrics],
        "leads": [l.__dict__ for l in leads],
        "gaps": [g.__dict__ for g in (m_gaps + l_gaps)],
    }
    runs.save_run(run)
    act.note(f"Uploaded {platform} export — {len(metrics)} metrics, {len(leads)} leads",
             run_id=run["id"])
    return {
        "dataset_id": run["id"],
        "platform": platform,
        "metrics": len(metrics),
        "leads": len(leads),
        "gaps": run["gaps"],
    }


@router.post("/mr/ingest-sheet")
def ingest_sheet(
    response: Response,
    body: dict | None = None,
    user=Depends(get_current_user),
    act: Activity = trail.records("ingest_sheet", "Pulled the live sheet"),
):
    """Pull the live Google-Sheets performance tracker into datasets.

    Body (all optional): ``{"gid": "...", "brand": "...", "year": 2026,
    "force": true}``.
    With a ``gid`` → that single tab is pulled. With no gid → the whole workbook
    is scanned and every performance-tracker tab is ingested (auto-discovery;
    non-tracker tabs are skipped). Each tab becomes one dataset run of
    channel-aggregate monthly metrics.

    The data lands under the WORKSPACE key (``_ws``), so a pull by any member
    refreshes what every member reads.

    Status is honest: 200 clean, 207 some component degraded (details in
    ``degraded``), 502 the pull failed and NOTHING was changed, 409 another
    pull for this workspace is mid-flight.

    **200 with ``status: "fresh"``** means the pull was NOT run: the workspace
    was pulled inside the cooldown (``MR_PULL_COOLDOWN_SECONDS``, default 120s),
    so nothing was fetched and ``last_pulled_at`` names when the data on screen
    was — see :func:`_pull_gate`.

    **While the workbook data is shared** (``workspace.is_shared()``), everything
    that widens what one request may do to the WHOLE team's dashboard is an
    admin/creator's, and a member is refused 403 — before the store or the lock
    is touched — with a plain-language reason:

    * ``"force": true`` — the cooldown is the only limiter on Google fetches. An
      admin's forced pull is still refused (429 + ``Retry-After``) inside the
      floor ``MR_FORCE_PULL_FLOOR_SECONDS`` (default 30s), and never skips the
      in-flight lock (409).
    * a ``gid`` (single-tab) pull — see :func:`_pull_single_tab`. No member-facing
      screen sends one; the auto-discovery pull is what everyone uses.

    Unshared, none of that applies: the workspace is the caller's own and this
    route behaves exactly as it did before the shared workspace existed."""
    body = body or {}
    year = int(body.get("year") or mr_config.SHEETS_YEAR)
    gid = body.get("gid")
    # ``is True``, not ``bool(...)``: the string "false" is truthy, and a flag that
    # skips a safety check should only ever fire on a real true.
    force = body.get("force") is True

    if force:
        _require_admin_while_shared(user, _FORCE_REFUSED)
    if gid:
        _require_admin_while_shared(user, _SINGLE_TAB_REFUSED)

    try:
        if gid and mr_workspace.is_shared():
            result = _ingest_single_tab(_ws(user), str(gid), year, brand=body.get("brand"),
                                        created_by=user["id"], force=force)
        elif gid:
            # UNSHARED: the caller's own workspace, exactly as before. (Shared,
            # this path would file the tab under ``sheets:<gid>`` — a second
            # vendor beside the pull's own ``sheets:<Title>`` row.)
            src = SheetsSource(
                mr_config.SHEETS_SPREADSHEET_ID, str(gid), year=year, brand=body.get("brand")
            )
            try:
                metrics, gaps = src.fetch_campaign_metrics(_FULL_RANGE)
            except Exception as exc:  # auth/network/format — honest 502, nothing written
                logger.warning("MR single-tab pull failed for gid %s: %s", gid, exc)
                raise HTTPException(502, f"Could not pull tab {gid}: {exc}") from exc
            row, _durable = _persist_sheet_dataset(_ws(user), str(gid), metrics, gaps)
            result = {"tabs": [row], "status": "ok", "ingested": 1, "failed": 0, "degraded": []}
        else:
            result = _ingest_sheet_all(_ws(user), year, force=force)
    except SheetPullBusy as exc:
        raise HTTPException(409, str(exc)) from exc
    except SheetPullTooSoon as exc:
        raise HTTPException(429, str(exc),
                            headers={"Retry-After": str(exc.retry_after)}) from exc
    except SheetTabRefused as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except SheetPullError as exc:
        raise HTTPException(
            502, f"Sheet pull failed — your existing data was left untouched. {exc}"
        ) from exc

    response.status_code = _PULL_HTTP_STATUS[result["status"]]
    if result["status"] == "fresh":
        # Nothing was pulled, so the trail must not claim a pull happened.
        act.note("Sheet pull skipped — the workspace was already pulled moments ago",
                 status="fresh")
    else:
        ok = len(result["tabs"]) - result["failed"]
        act.note(f"Sheet pull ({result['status']}) — {ok}/{len(result['tabs'])} tabs ingested",
                 status=str(result["status"]))
    return {"spreadsheet_id": mr_config.SHEETS_SPREADSHEET_ID, "year": year, **result}


def _persist_sheet_dataset(user_id: str, label: str, metrics, gaps, *,
                           created_by=None) -> tuple[dict, bool]:
    """Write one tab's dataset run. Returns ``(row, durable)`` — see
    ``runs.save_run``: ``durable`` is False when only the ephemeral disk copy
    was written, which is what gates the swap's delete pass.

    ``label`` is the tab's TITLE — the platform is ``sheets:<title>``, which is
    the key the read path keeps the newest run of, so the same tab always lands
    on the same key. ``created_by`` is set only by a single-tab pull (a person
    pressed the button); the full pull, which the cron also runs, has no author
    and leaves it off."""
    run = {
        "id": runs.new_run_id(),
        "kind": "dataset",
        "user_id": user_id,
        "agent_id": MR_AGENT_ID,
        "platform": f"sheets:{label}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "metrics": [m.__dict__ for m in metrics],
        "leads": [],
        "gaps": [g.__dict__ for g in gaps],
    }
    if created_by is not None:
        run["created_by"] = created_by
    durable = runs.save_run(run)
    return ({"tab": label, "dataset_id": run["id"], "metrics": len(metrics),
             "gaps": run["gaps"]}, durable)


#: Seconds a workspace's data counts as fresh after a pull. Every member of a
#: shared workspace can press Pull, and the in-process lock below only
#: serialises pulls on ONE Cloud Run instance, so without a freshness gate the
#: team can fan out into a Google fetch each. ``MR_PULL_COOLDOWN_SECONDS``
#: overrides it; ``0`` disables the gate.
_DEFAULT_PULL_COOLDOWN_SECONDS = 120.0

#: The floor a FORCED pull (admin/creator only) cannot cross: seconds since the
#: last pull inside which even ``force`` is refused (429 + ``Retry-After``).
#: ``force`` exists to skip the cooldown — without a floor beneath it, one
#: scripted loop of forced pulls is an unlimited Google fetch. It is much
#: shorter than the cooldown on purpose: it stops a hammer, not a person who
#: wants fresher numbers. ``MR_FORCE_PULL_FLOOR_SECONDS`` overrides it; ``0``
#: disables it.
_DEFAULT_FORCE_PULL_FLOOR_SECONDS = 30.0

#: Rows read from a tab, the same cap ``fetch_all_trackers`` reads each with —
#: so a single-tab pull sees exactly what the full pull would have.
_TRACKER_MAX_ROWS = 200


def _seconds_from_env(name: str, default: float) -> float:
    """A non-negative, finite number of seconds from the environment, read on
    every call. Anything unparseable, negative or non-finite falls back to
    ``default`` rather than turning a limiter into a permanent block (``inf``) or
    silently off."""
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        logger.warning("%s is not a number (%r) — using %.0fs", name, raw, default)
        return default
    if not math.isfinite(value) or value < 0:
        return default
    return value


def _pull_cooldown_seconds() -> float:
    return _seconds_from_env("MR_PULL_COOLDOWN_SECONDS", _DEFAULT_PULL_COOLDOWN_SECONDS)


def _force_pull_floor_seconds() -> float:
    return _seconds_from_env("MR_FORCE_PULL_FLOOR_SECONDS", _DEFAULT_FORCE_PULL_FLOOR_SECONDS)


def _stamp_of(value) -> datetime | None:
    """``generated_at`` as an aware datetime, or ``None`` when it is not one.
    A naive stamp is read as UTC, which is what every writer here produces."""
    try:
        stamp = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    return stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc)


def _last_pull_at(workspace: str) -> datetime | None:
    """When this workspace was last fully PULLED, or ``None``.

    ONLY ``official_spend`` runs count, on purpose: that kind is written by
    :func:`_pull_and_swap` and by nothing else. A ``dataset`` run is not evidence
    of a pull — an upload is not one, and a single-tab pull writes a fresh
    ``sheets:<tab>`` dataset too. If either advanced this clock, a request every
    few seconds would keep the cooldown permanently "fresh": every cron fire
    would answer 200 ``fresh``, fetch nothing and sweep nothing while the team's
    tracker data froze with no alert.

    The cost of that rule, stated: a workbook with no roll-up tab, or one whose
    roll-up was rejected at reconciliation, writes no new ``official_spend`` run,
    so its clock does not advance and it has no cooldown until a healthy pull.
    That state is itself a degraded (207) pull the cron already shouts about.

    Raises :class:`runs.RunStoreError` when the store cannot be read.
    """
    newest: datetime | None = None
    for run in runs.list_runs(workspace, kind="official_spend"):
        stamp = _stamp_of(run.get("generated_at"))
        if stamp is not None and (newest is None or stamp > newest):
            newest = stamp
    return newest


def _pull_gate(workspace: str, *, force: bool) -> dict | None:
    """The limiter in front of a pull (full or single-tab) while the workspace
    is SHARED. Returns the ``status: "fresh"`` answer, or ``None`` to go ahead;
    raises :class:`SheetPullTooSoon` for a forced pull inside the floor.

    * **Not forced**, pulled inside the cooldown -> ``fresh``: nothing is
      fetched, the body says so (``ingested: 0``, ``tabs: []``) and names when
      the data on screen was pulled. Honest, not fake success.
    * **Forced** (admin/creator only — the route refuses everyone else before it
      gets here) -> the cooldown is skipped, the FLOOR is not. Inside it the
      pull is refused outright, so a scripted caller backs off honestly instead
      of being told the data is fine.

    Only ever consulted while shared. With one key per caller there is one puller
    per key and nothing to protect, so the unshared mode pulls exactly as it
    always did. An unreadable store never blocks or skips — it falls through to
    the pull, whose own read of the existing runs answers the honest 502.
    """
    try:
        last = _last_pull_at(workspace)
    except runs.RunStoreError:
        return None
    if last is None:
        return None
    age = (datetime.now(timezone.utc) - last).total_seconds()
    if age < 0:  # a stamp from the future is clock skew, not a recent pull
        return None
    if force:
        floor = _force_pull_floor_seconds()
        if floor > 0 and age < floor:
            retry_after = max(1, math.ceil(floor - age))
            raise SheetPullTooSoon(
                f"The tracker was pulled {int(age)} seconds ago. Even a forced pull "
                f"waits {floor:g} seconds after the last one — try again in "
                f"{retry_after} seconds.", retry_after)
        return None
    cooldown = _pull_cooldown_seconds()
    if cooldown > 0 and age < cooldown:
        return {"tabs": [], "status": "fresh", "ingested": 0, "failed": 0,
                "degraded": [], "last_pulled_at": last.isoformat()}
    return None


def _acquire_pull_lock(workspace: str) -> threading.Lock:
    """The workspace's pull lock, ACQUIRED — or :class:`SheetPullBusy`. Every pull
    takes it before anything else, ``force`` included: a forced pull is exempt
    from the cooldown, never from the rule that two pulls must not interleave
    their write and delete passes over the same rows."""
    lock = _pull_lock(workspace)
    if not lock.acquire(blocking=False):
        raise SheetPullBusy(
            "A sheet pull for this workspace is already running. Two overlapping "
            "pulls interleave their writes and deletes — try again in a moment."
        )
    return lock


def _ingest_sheet_all(user_id: str, year: int, *, force: bool = False) -> dict:
    """Auto-discovery pull of every tracker tab for one workspace (UI and cron
    path). ``user_id`` is the WORKSPACE key the caller resolved.

    Serialised per workspace, then handed to :func:`_pull_and_swap`. Returns
    ``{"tabs", "status", "ingested", "failed", "degraded"}`` — or, while shared
    and the workspace was pulled inside the cooldown, the ``"fresh"`` answer of
    :func:`_pull_gate`, having fetched nothing. Raises ``SheetPullError``
    (nothing touched), ``SheetPullBusy`` or ``SheetPullTooSoon``.

    The gate sits INSIDE the lock so two pulls on this instance cannot both pass
    it; a second instance can, which the fetch-then-swap in
    :func:`_pull_and_swap` and the stale-duplicate sweep at its end make
    harmless rather than impossible. The cron never forces.
    """
    lock = _acquire_pull_lock(user_id)
    try:
        if mr_workspace.is_shared():
            fresh = _pull_gate(user_id, force=force)
            if fresh is not None:
                return fresh
        return _pull_and_swap(user_id, year)
    finally:
        lock.release()


def _ingest_single_tab(workspace: str, gid: str, year: int, *, brand, created_by,
                       force: bool) -> dict:
    """A single-tab pull while the workspace is SHARED — under the same lock and
    the same cooldown/floor as :func:`_ingest_sheet_all`, so it is not a side
    door around either."""
    lock = _acquire_pull_lock(workspace)
    try:
        fresh = _pull_gate(workspace, force=force)
        if fresh is not None:
            return fresh
        return _pull_single_tab(workspace, gid, year, brand=brand, created_by=created_by)
    finally:
        lock.release()


def _pull_single_tab(workspace: str, gid: str, year: int, *, brand, created_by) -> dict:
    """Pull ONE tab of the primary tracker into the shared workspace, by ``gid``.

    Built to land exactly where auto-discovery would have put the same tab, and to
    refuse everything auto-discovery would have skipped:

    * The tab's TITLE is resolved from the workbook (the Sheets API, the way
      ``fetch_all_trackers`` enumerates tabs) and the run is labelled
      ``sheets:<title>``. The old label, ``sheets:<gid>``, was a different key
      space: the read path keeps the newest run PER PLATFORM, so the same tab
      counted twice — once as ``sheets:Meta 360 RA``, once as ``sheets:42``.
      Under the tab's own title the new run SUPERSEDES the pull's, and the
      stale-duplicate sweep retires the old row.
    * The roll-up tab is refused at write time (:func:`is_rollup_tab`: by name
      AND by an "All"/"Overall" A1 scope), 422. Its numbers are the sum of the
      vendor tabs, so ingesting it is the double count the read-path guard
      (``is_rollup_platform``) exists to catch — and that guard keys on the
      LABEL, which a numeric gid used to defeat.
    * A hidden tab is refused (archives, never a live vendor), a gid that names
      no tab is a 404, and a tab that parses to NO metrics is a 422 — writing an
      empty run under the tab's title would supersede the good one and blank
      that vendor for everyone.

    Nothing is written on any refusal. A failed workbook read is a
    :class:`SheetPullError` (502), never a fallback label. ``created_by`` is the
    authenticated caller, server-derived.
    """
    sid = mr_config.SHEETS_SPREADSHEET_ID
    try:
        tabs = workbook_meta(sid)["tabs"]
    except Exception as exc:
        logger.warning("MR single-tab pull: could not list the tracker's tabs: %s", exc)
        raise SheetPullError(f"could not read the tracker's tab list: {exc}") from exc
    tab = next((t for t in tabs if str(t.get("gid")) == gid), None)
    if tab is None:
        raise SheetTabRefused(404, "That tab was not found in the tracker workbook.")
    title = str(tab.get("title") or "").strip()
    if not title:
        raise SheetTabRefused(422, "That tab has no title to file its numbers under.")
    if tab.get("hidden"):
        raise SheetTabRefused(
            422, f"'{title}' is a hidden tab. Hidden tabs are archives, never a live "
                 "vendor, so they are not pulled.")

    try:
        rows = fetch_tab_values(sid, title)[:_TRACKER_MAX_ROWS]
    except Exception as exc:
        logger.warning("MR single-tab pull failed for tab %r: %s", title, exc)
        raise SheetPullError(f"could not read tab '{title}': {exc}") from exc
    if is_rollup_tab(title, rows):
        raise SheetTabRefused(
            422, f"'{title}' is the roll-up tab. Its numbers are the sum of the vendor "
                 "tabs, so pulling it as a vendor would count every dollar twice.")

    metrics, gaps = parse_tracker(rows, year, brand)
    if not metrics:
        why = "; ".join(g.message for g in gaps[:2]) or "no month columns with data"
        raise SheetTabRefused(
            422, f"'{title}' does not read as a performance tracker ({why}), so nothing "
                 "was changed.")

    degraded = sorted({g.message for g in gaps if "column band" in g.message})
    row, durable = _persist_sheet_dataset(workspace, title, metrics, gaps,
                                          created_by=created_by)
    if durable:
        # The new run shares this tab's key with the pull's own, so it already
        # wins every read; retiring the superseded row keeps ``/mr/datasets``
        # from listing one tab twice.
        _sweep_superseded_tracker_runs(workspace)
    else:
        degraded.append("the tab could not be stored durably — the previous run was kept")
    return {"tabs": [row], "status": "partial" if degraded else "ok", "ingested": 1,
            "failed": 0, "degraded": degraded}


def _sweep_superseded_tracker_runs(workspace: str) -> int:
    """Retire every ``sheets:*`` dataset a newer run of the same tab replaced.

    The in-process lock cannot stop two Cloud Run instances pulling the same
    workspace at once, and each one's swap only retires what it read BEFORE it
    wrote. Both can therefore leave a run of the same tab behind. Readers never
    see the duplicate (``_latest_datasets`` takes the newest per platform), so
    this is housekeeping, not a correctness fix — but it is what keeps a busy
    shared workspace from accumulating one stale copy of every tab per race.

    Per platform the newest run always survives, so this can never empty a tab.
    Only ``sheets:*`` is touched: uploads (``google_ads``, ``pdf:*``) are people's
    contributions and are never swept. Best effort — an unreadable store leaves
    the duplicates for the next pull, which repeats this. Returns how many it
    retired.
    """
    try:
        current = runs.list_runs(workspace, kind="dataset")
    except runs.RunStoreError as exc:
        logger.warning("MR pull: stale-duplicate sweep skipped for %s: %s", workspace, exc)
        return 0
    seen: set[str] = set()
    retired = 0
    for run in current:  # newest first — the first per platform is the survivor
        platform = str(run.get("platform") or "")
        if not platform.startswith("sheets:") or not run.get("id"):
            continue
        if platform not in seen:
            seen.add(platform)
            continue
        try:
            runs.delete_run(run["id"])
        except Exception:  # housekeeping must never fail the pull that just succeeded
            logger.warning("MR pull: could not retire superseded run %s", run["id"],
                           exc_info=True)
            continue
        retired += 1
    if retired:
        logger.info("MR pull: retired %d superseded tracker dataset(s) for %s",
                    retired, workspace)
    return retired


def _pull_and_swap(user_id: str, year: int) -> dict:
    """FETCH-THEN-SWAP the whole workspace.

    Everything is read from Google FIRST; the previous runs are deleted only
    once their replacements are written. The old order (delete every
    ``sheets:*`` dataset plus the official and lead runs, then fetch) meant one
    429 or one revoked share left the dashboard permanently blank — ``mr_runs``
    is the only copy of parsed tracker state — and still answered 200.

    Nothing is deleted for a component that could not be re-fetched, so a
    Sheets blip now costs at most a stale figure, never a missing one.
    """
    sid = mr_config.SHEETS_SPREADSHEET_ID
    degraded: list[str] = []

    # ---- phase 1: fetch. No write and no delete happens while this runs. ----
    try:
        fetched = list(fetch_all_trackers(sid, year))
    except Exception as exc:
        logger.exception("MR sheet pull: tracker fetch failed for user %s", user_id)
        raise SheetPullError(f"could not read the tracker tabs: {exc}") from exc

    # Layout problems the parser worked around (a month repeated in a second
    # column band) are the early warning that a tab was restructured. They are
    # already on each dataset's gap list; promoting them to `degraded` is what
    # puts them in front of a human before the figures drift.
    degraded.extend(sorted({
        g.message for f in fetched for g in (f.get("gaps") or [])
        if "column band" in getattr(g, "message", "")
    }))

    # The Overall tab's own team-level rows — the official headline figures the
    # console must match (the roll-up aggregates ledger/raw sources no vendor
    # tab carries). Contract C-4: a raise means the Sheets call failed and the
    # previous official run must survive; ``{}`` means this workbook genuinely
    # has no roll-up tab and the previous run is correctly retired. Those two
    # used to be the same value, so a blip silently zeroed the headline.
    official: dict | None
    official_layout: list[str] = []
    try:
        official = fetch_official_totals(sid, year, warnings=official_layout)
        degraded.extend(official_layout)
    except Exception as exc:
        official = None
        degraded.append(f"official totals unavailable ({exc}) — kept the previous figures")
        logger.warning("MR sheet pull: official totals unavailable for %s: %s", user_id, exc)

    # Self-check before anything is believed: the roll-up cannot report less
    # media spend than the vendor tabs it aggregates. When it does, the read is
    # on the wrong cells, and publishing it as "the official figure" is exactly
    # the silent-wrong-number failure this pull exists to avoid. Treated like a
    # Sheets failure — previous figures survive and the reason is named.
    if official and fetched:
        mismatches = reconcile_official_spend(fetched, official, through=date.today())
        if mismatches:
            official = None
            degraded.append(
                "official totals rejected — they do not reconcile with the vendor "
                "tabs, so the previous figures were kept. " + "; ".join(mismatches)
            )
            logger.error("MR sheet pull: official totals failed reconciliation for %s: %s",
                         user_id, mismatches)

    # ---- phase 2: swap. Replacements are written first and the superseded
    # runs deleted after, so the workspace is never empty at any instant.
    # (Same-tab runs share a "sheets:<tab>" platform key and the read path takes
    # the newest per key, so the overlap can never double-count.) ----
    swap_datasets = bool(fetched)
    if not swap_datasets:
        # A workbook that had eleven vendor tabs yesterday and none today is far
        # more likely a permissions/format change than a real deletion. Keep
        # what we have and say so rather than blanking on a maybe.
        degraded.append("no tracker tabs matched — kept the previous datasets")
    swap_kinds = {"official_spend"} if official is not None else set()
    # Split per component: each half is retired only once ITS replacement is
    # durably stored, so a per-document write failure can never take the
    # originals with it.
    # Read the superseded set BEFORE writing anything. A store failure here must
    # abort the swap: proceeding would write replacements and then never retire
    # what they replace, leaving the workspace double-counting for ever.
    try:
        # Only the two kinds this swap can supersede — report runs are never
        # retired by a pull, so there is no reason to ship them here.
        previous = runs.list_runs(user_id, kind=("dataset", "official_spend"))
    except runs.RunStoreError as exc:
        raise SheetPullError(
            f"could not read the existing runs ({exc}) — nothing was changed") from exc
    superseded_datasets = [
        r["id"] for r in previous
        if swap_datasets and r.get("kind") == "dataset"
        and str(r.get("platform", "")).startswith("sheets:")
    ]
    superseded_official = [r["id"] for r in previous if r.get("kind") in swap_kinds]

    results: list[dict] = []
    datasets_durable = True
    for f in fetched:
        row, durable = _persist_sheet_dataset(user_id, f["tab"], f["metrics"], f["gaps"])
        results.append(row)
        datasets_durable = datasets_durable and durable
    official_durable = True  # an empty roll-up writes nothing, so nothing can fail
    if official:
        official_durable = runs.save_run({
            "id": runs.new_run_id(), "kind": "official_spend", "user_id": user_id,
            "agent_id": MR_AGENT_ID, "platform": "sheets-official",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            # "months" keeps the legacy spend-only shape for old readers.
            "months": {k: v["spend"] for k, v in official.items() if "spend" in v},
            "totals": official,
        })
        results.append({"tab": "Official totals (Overall Report)", "months": len(official)})
    elif official is None:
        results.append({"tab": "Official totals (Overall Report)",
                        "error": "Sheets read failed — previous figures kept"})

    # A replacement that only reached this instance's ephemeral disk is not a
    # replacement: the next deploy loses it, and deleting the superseded run
    # would have destroyed the durable copy. Keep the originals and say why.
    if not datasets_durable:
        superseded_datasets = []
        degraded.append("tracker datasets could not be stored durably — "
                        "kept the previous datasets")
    if not official_durable:
        superseded_official = []
        degraded.append("official totals could not be stored durably — "
                        "kept the previous figures")
    for run_id in superseded_datasets + superseded_official:
        runs.delete_run(run_id)
    # Only after a durable write of real replacements: sweep whatever a pull on
    # another instance left behind for the same tabs. See the helper.
    if swap_datasets and datasets_durable:
        _sweep_superseded_tracker_runs(user_id)

    # ---- phase 3: lead analysis. Runs AFTER the tracker swap so the
    # QL-ratio/booking-rate rule joins against the fresh funnel counts. Its own
    # fetch-then-swap: the previous summary only goes once a new one exists. ----
    try:
        built = _build_lead_analysis(user_id, year)
    except Exception as exc:
        degraded.append(f"lead analysis unavailable ({exc}) — kept the previous summary")
        results.append({"tab": "Lead analysis", "error": str(exc)})
        logger.warning("MR sheet pull: lead analysis failed for %s: %s", user_id, exc)
    else:
        # After the tracker swap: a store failure here must not cost us the
        # freshly built summary, so keep the previous one instead of retiring it.
        try:
            stale_leads = [r["id"] for r in runs.list_runs(user_id, kind="lead_analysis")]
        except runs.RunStoreError as exc:
            stale_leads = []
            degraded.append(f"could not read the previous lead analysis ({exc}) — kept it")
        if built:
            lead_run, row = built
            results.append(row)
            if not runs.save_run(lead_run):
                stale_leads = []
                degraded.append("lead analysis could not be stored durably — "
                                "kept the previous summary")
        for run_id in stale_leads:
            runs.delete_run(run_id)

    return {
        "tabs": results,
        "status": "partial" if degraded else "ok",
        "ingested": len(fetched),
        "failed": sum(1 for r in results if "error" in r),
        "degraded": degraded,
    }


def _cron_status(response: Response, out: dict, failures: list[str], *, fatal: bool) -> dict:
    """Stamp an honest HTTP status on a cron result.

    Cloud Scheduler only ever looks at the status code, so a 200 carrying "every
    item failed" in its body is a job that can be dead for weeks with nobody
    paged. 502 = nothing worked (the scheduler retries and the failed-job alert
    fires), 207 = some of it worked (kept 2xx on purpose so a permanently bad
    item can't trigger an endless retry loop — it shouts in the log instead),
    200 = clean."""
    out["errors"] = failures
    if fatal:
        out["status"] = "failed"
        response.status_code = 502
        logger.error("MR cron refresh FAILED: %s", "; ".join(failures) or "unknown")
    elif failures:
        out["status"] = "partial"
        response.status_code = 207
        logger.warning("MR cron refresh degraded: %s", "; ".join(failures))
    else:
        out["status"] = "ok"
    return out


@router.post("/mr/cron/refresh")
def cron_refresh(request: Request, response: Response,
                 act: Activity = trail.records("cron_refresh", "Scheduled MR refresh",
                                               unit=JOB, actor=CRON)):
    """Scheduled full refresh: sheet pull + daily snapshot capture + GCS export.

    Authenticated by the MR_CRON_KEY shared secret (Cloud Scheduler can't hold a
    Firebase user session). The pull runs for MR_CRON_USER_ID's workspace — and
    since 2026-08-21 that means it also evaluates red flags against THAT
    workspace's targets, because the runs it writes are stamped with that
    user_id. Since the shared workspace, every signed-in member reads those same
    runs (``_ws``), so this is the pull that keeps the whole team's dashboard
    fresh; the key it writes under goes through ``workspace_id`` so an explicit
    ``MR_WORKSPACE_ID`` cannot make it write a key nobody reads. With
    MR_CRON_USER_ID unset the pull is skipped outright (below) rather than run
    against a deployment-wide default, so there is no path where the cron flags
    one desk's data with another desk's red lines. The snapshot capture and
    exports are user-independent.

    A pull skipped because a member pulled moments ago (``status: "fresh"``) is
    clean, not degraded: its ``degraded`` list is empty and the data is current.

    Reports 200/207/502 per :func:`_cron_status` — a refresh where every stage
    failed is never a 200."""
    key = os.environ.get("MR_CRON_KEY", "")
    if not key:
        raise HTTPException(503, "MR_CRON_KEY not configured on this deployment")
    if not hmac.compare_digest(request.headers.get("x-cron-key", ""), key):
        raise HTTPException(403, "bad cron key")

    today = date.today()
    out: dict = {"date": today.isoformat()}
    failures: list[str] = []
    fatal = False

    # Stripped at the source, and blank counts as UNSET. A whitespace-only value
    # used to pass ``if not uid`` and then blow up in ``workspace_id`` (a 500,
    # outside the try below); a stray trailing space wrote the whole pull under a
    # key ("abc ") that nobody reads ("abc").
    uid = (os.environ.get("MR_CRON_USER_ID") or "").strip()
    if not uid:
        out["pull"] = "skipped (MR_CRON_USER_ID unset)"
        failures.append("sheet pull skipped: MR_CRON_USER_ID unset")
    else:
        uid = mr_workspace.workspace_id(uid)
        try:
            out["pull"] = _ingest_sheet_all(uid, mr_config.SHEETS_YEAR)
        except SheetPullBusy as exc:
            # Overlapping fire — the in-flight pull is doing the work. Not fatal.
            out["pull"] = {"status": "busy", "error": str(exc)}
            failures.append(f"sheet pull skipped: {exc}")
        except SheetPullError as exc:
            out["pull"] = {"status": "failed", "error": str(exc)}
            failures.append(f"sheet pull failed: {exc}")
            fatal = True  # the primary job of this cron did not happen
        else:
            failures.extend(out["pull"]["degraded"])

    try:
        grids = _workbook_grids()
    except Exception as exc:
        out["capture_error"] = str(exc)
        failures.append(f"snapshot capture failed: {exc}")
        # The fire still happened and still half-failed; recording it is how the
        # next person finds out the workbook stopped being readable.
        act.note(f"Scheduled refresh for {today.isoformat()} — workbook unreadable: {exc}",
                 status="failed")
        return _cron_status(response, out, failures, fatal=fatal or not uid)
    try:
        out["capture"] = mr_snapshots.capture_workbook(
            grids, year=mr_config.SHEETS_YEAR, today=today)
    except Exception as exc:
        out["capture_error"] = str(exc)
        failures.append(f"snapshot capture failed: {exc}")
    try:
        out["exported"] = mr_snapshots.export_all_to_gcs(today)
    except Exception as exc:
        out["export_error"] = str(exc)
        failures.append(f"snapshot export failed: {exc}")
    act.note(f"Scheduled refresh for {today.isoformat()}"
             + (f" — {len(failures)} degraded" if failures else ""),
             status="failed" if fatal else "completed")
    return _cron_status(response, out, failures, fatal=fatal)


def _may_delete_dataset(run: dict, user: dict) -> bool:
    """Whether THIS caller may delete this dataset run.

    The listing is workspace-wide now, so "it is in your list" no longer means
    "you put it there". Mirrors the ``/mr/sources`` rule (``added_by`` /
    ``can_remove``), which is the precedent for a shared list with owned rows:

    * not in the caller's workspace -> no (also what makes a foreign id a 404);
    * an admin or creator -> yes, including pull-produced and pre-attribution
      rows, which nobody else may retire;
    * an UNSHARED workspace -> yes: the workspace is the caller's own, so every
      row in it is theirs and this is exactly today's behaviour;
    * shared, a ``sheets:*`` pull -> no: it is the team's live tracker data, and
      the next pull would only recreate it (that includes a single-tab pull, whose
      ``created_by`` is an admin — the platform, not the author, decides);
    * shared, an upload -> only the person who uploaded it (``created_by``). Only
      an admin can upload while shared, so for a non-admin this is the person who
      WAS one when they did; a row with no ``created_by`` predates attribution and
      has no owner to name.
    """
    if run.get("user_id") != _ws(user):
        return False
    if _is_workspace_admin(user):
        return True
    if not mr_workspace.is_shared():
        return True
    if str(run.get("platform") or "").startswith("sheets:"):
        return False
    who = str(run.get("created_by") or "").strip()
    return bool(who) and who == str(user["id"])


def _dataset_delete_refusal(run: dict) -> str:
    """The 403 wording — plain language, and says who CAN do it."""
    if str(run.get("platform") or "").startswith("sheets:"):
        return ("This dataset is pulled from the live tracker sheet, so only an "
                "admin can delete it. The next pull would bring it back anyway.")
    if not str(run.get("created_by") or "").strip():
        return ("This dataset was uploaded before we started recording who added "
                "it. An admin can delete it.")
    return "Only the person who uploaded this dataset, or an admin, can delete it."


@router.get("/mr/datasets")
def datasets(user=Depends(get_current_user)):
    """Every dataset in the caller's workspace — pulled tabs and uploads alike.

    Each row says who added it (``created_by``: the uploader, or the admin who
    ran a single-tab pull; absent on rows that predate attribution and on rows a
    full pull — which the cron also runs — wrote), whether THIS caller may delete it
    (``can_delete``, so the console can hide a button that would only earn a
    403) and whether it is ``superseded`` — a NEWER run of the same platform
    exists, so this one no longer contributes to any figure. The read path keeps
    only the newest run per platform (``_latest_datasets``), which is why two
    ``google_ads`` uploads shadow each other and only the later one is on the
    dashboard; without this flag the list showed both as if both counted."""
    listed = runs.list_runs(_ws(user), kind="dataset")  # newest first
    seen: set[str] = set()
    out = []
    for r in listed:
        platform = r.get("platform", r["id"])  # the key ``_latest_datasets`` uses
        out.append({
            "id": r["id"],
            "platform": r.get("platform"),
            "generated_at": r.get("generated_at"),
            "metrics": len(r.get("metrics", [])),
            "leads": len(r.get("leads", [])),
            "gaps": r.get("gaps", []),
            "created_by": r.get("created_by"),
            "can_delete": _may_delete_dataset(r, user),
            "superseded": platform in seen,
        })
        seen.add(platform)
    return out


@router.delete("/mr/datasets/{dataset_id}")
def delete_dataset(dataset_id: str, user=Depends(get_current_user),
                   act: Activity = trail.records("dataset_deleted", "Deleted a dataset",
                                                 unit=CHANGE)):
    """Remove one ingested file/pull from the workspace (its numbers leave the
    Overview and future reports immediately).

    404 for anything outside the caller's workspace, so a foreign id is
    indistinguishable from an invented one. 403 — not 404 — for a dataset that IS
    in the caller's workspace but is not theirs to delete: the row is in their
    own ``GET /mr/datasets`` listing, so a 404 would leak nothing and lie about a
    dataset they can plainly see (the same argument as ``DELETE /mr/sources``).
    A refusal returns before ``act.note``, so it records no "deleted" activity.
    """
    run = runs.get_run(dataset_id)
    if not run or run.get("user_id") != _ws(user) or run.get("kind") != "dataset":
        raise HTTPException(404, "dataset not found")
    if not _may_delete_dataset(run, user):
        raise HTTPException(403, _dataset_delete_refusal(run))
    runs.delete_run(dataset_id)
    act.note(f"Dataset deleted — {run.get('platform') or dataset_id}", run_id=dataset_id)
    return {"deleted": dataset_id}


_PDF_EXTRACT_PROMPT = """You are a marketing data extractor. Below is text from a
marketing performance PDF. Find campaign/channel performance figures and reply
with ONLY a JSON array (no prose). One object per channel or campaign row:
{"channel": "Google|META|Email|Websites|Organic|...", "campaign": "<name>",
 "date": "YYYY-MM-DD", "spend": <number>, "leads": <int>,
 "qualified_leads": <int>, "demos_booked": <int>, "demos_completed": <int>}
Use 0 for counts the document doesn't state; use the document's period start for
"date" (default {today} if none is stated). If the document has no usable
marketing metrics, reply [].

Text:
{text}
"""


def _metrics_from_pdf(text: str, today: date) -> list[CampaignMetric]:
    from marketing_research_agent import analysis as mr_analysis

    prompt = _PDF_EXTRACT_PROMPT.replace("{today}", today.isoformat()).replace("{text}", text[:12000])
    raw = mr_analysis.llm_json(prompt)
    out: list[CampaignMetric] = []
    if not isinstance(raw, list):
        return out
    for r in raw:
        if not isinstance(r, dict):
            continue
        try:
            when = date.fromisoformat(str(r.get("date", ""))[:10])
        except ValueError:
            when = today.replace(day=1)
        try:
            channel = str(r.get("channel") or "Other").strip() or "Other"
            out.append(CampaignMetric(
                channel=channel,
                campaign=str(r.get("campaign") or channel),
                utm_source=channel.lower(),
                utm_medium="pdf",
                utm_campaign=str(r.get("campaign") or channel),
                spend=float(r.get("spend") or 0),
                leads=int(r.get("leads") or 0),
                qualified_leads=int(r.get("qualified_leads") or 0),
                demos_booked=int(r.get("demos_booked") or 0),
                demos_completed=int(r.get("demos_completed") or 0),
                date=when,
            ))
        except (TypeError, ValueError):
            continue
    return out


@router.post("/mr/ingest-pdf")
async def ingest_pdf(file: UploadFile = File(...), user=Depends(get_current_user),
                     act: Activity = trail.records("ingest_pdf", "Parsed an uploaded PDF")):
    """Upload a PDF report: text is extracted locally, metrics are parsed by the
    LLM into the canonical schema and saved as a dataset.

    Same rule as ``/mr/ingest``: while the workbook data is shared the result
    joins the whole team's dashboard, so only an admin/creator may upload (403
    otherwise) — checked first, which also means a member's request never reaches
    the billed model call."""
    import io as _io

    _require_admin_while_shared(user, _UPLOAD_REFUSED)
    content = await file.read()
    name = file.filename or "report.pdf"
    if not name.lower().endswith(".pdf"):
        raise HTTPException(400, "expected a .pdf file")
    try:
        from pypdf import PdfReader
        reader = PdfReader(_io.BytesIO(content))
        text = "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    except Exception as exc:
        raise HTTPException(400, f"could not read the PDF: {exc}")

    gaps: list[dict] = []
    metrics: list[CampaignMetric] = []
    if not text:
        gaps.append({"source": "pdf", "message": "no extractable text in the PDF (scanned image?)"})
    else:
        metrics = _metrics_from_pdf(text, date.today())
        if not metrics:
            gaps.append({"source": "pdf",
                         "message": "no campaign metrics could be parsed from this PDF"})

    run = {
        "id": runs.new_run_id(),
        "kind": "dataset",
        # The workspace key + who added it — see ``ingest``.
        "user_id": _ws(user),
        "created_by": user["id"],
        "agent_id": MR_AGENT_ID,
        "platform": f"pdf:{name}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "metrics": [m.__dict__ for m in metrics],
        "leads": [],
        "gaps": gaps,
    }
    runs.save_run(run)
    act.note(f"Parsed PDF “{name}” — {len(metrics)} metrics", run_id=run["id"])
    return {"dataset_id": run["id"], "platform": run["platform"],
            "metrics": len(metrics), "leads": 0, "gaps": gaps}


@router.get("/mr/overview")
def overview(user=Depends(get_current_user)):
    """Live dashboard state — latest-month KPIs vs 2026 goals. Persists nothing.

    The figures are the WORKSPACE's (``_ws``); the traffic lights are judged
    against the CALLER's own targets, which stay private."""
    return reports.overview(_load_dataset(_ws(user)), user["id"])


@router.get("/mr/lead-analysis")
def lead_analysis_view(user=Depends(get_current_user)):
    """The lead sheet's per-vendor Meeting Outcome / Deal Stage picture + the
    five lead-quality flags. Data refreshes with every sheet pull (UI or cron).

    Workspace-wide, and so are its flags: they were frozen at pull time against
    the workspace's own thresholds (see ``_build_lead_analysis``)."""
    run = _latest_lead_run(_ws(user))
    if not run:
        return {"has_data": False, "hint": (
            "No lead-analysis tab found yet. Connect the lead sheet from the Data tab "
            "(share it with the service account) and pull — the agent detects the tab "
            "by its columns automatically.")}
    summary = run.get("summary") or {}
    return {"has_data": bool(summary.get("months")),
            "generated_at": run.get("generated_at"),
            "source_label": run.get("source_label"), "tab": run.get("tab"),
            "gaps": run.get("gaps", []), **summary}


@router.get("/mr/trends")
def trends_endpoint(user=Depends(get_current_user)):
    """Monthly rollups + deterministic desk insights for the Overview board."""
    ws = _ws(user)
    latest = _latest_datasets(ws)
    vendor_datasets = [
        {"vendor": plat[7:] if str(plat).startswith("sheets:") else str(plat),
         "metrics": _rehydrate_metrics(run.get("metrics", []))}
        for plat, run in sorted(latest.items())
    ]
    official = _latest_official_run(ws)
    return mr_trends.build(vendor_datasets, today=date.today(),
                           official_spend=dict(official.get("months") or {}),
                           official_totals=dict(official.get("totals") or {}))


@router.post("/mr/snapshots/capture")
def snapshots_capture(user=Depends(get_current_user),
                      act: Activity = trail.records("snapshot", "Captured tab snapshots")):
    """Freeze today's MTD state of every tracker tab + refresh the GCS export.
    The daily cron target AND the UI's 'Snapshot now' button."""
    today = date.today()
    try:
        grids = _workbook_grids()
    except Exception as exc:
        raise HTTPException(502, f"Could not read the spreadsheet: {exc}")
    results = mr_snapshots.capture_workbook(grids, year=mr_config.SHEETS_YEAR, today=today)
    exported = mr_snapshots.export_all_to_gcs(today)
    act.note(f"Captured {len(results)} tab snapshots for {today.isoformat()}")
    return {"date": today.isoformat(), "tabs": results, "exported": exported}


@router.get("/mr/snapshots")
def snapshots_list(vendor: str | None = None, month: str | None = None,
                   user=Depends(get_current_user)):
    return mr_snapshots.list_snapshots(slug=vendor, month=month, meta_only=True)


@router.get("/mr/snapshots/deltas")
def snapshots_deltas(date_iso: str | None = None, user=Depends(get_current_user)):
    return mr_snapshots.deltas_for(date_iso)


@router.get("/mr/snapshots/portfolio")
def snapshots_portfolio(user=Depends(get_current_user)):
    """Official cross-vendor totals for the Vendors tab summary bar."""
    out = mr_snapshots.portfolio()
    if out is None:
        raise HTTPException(404, "no vendor snapshots yet")
    return out


@router.get("/mr/snapshots/vendor/{slug}")
def snapshots_vendor(slug: str, date_iso: str | None = None, user=Depends(get_current_user)):
    """Full per-vendor dossier: dates, the day's snapshot, its movement."""
    out = mr_snapshots.vendor_detail(slug, date_iso)
    if out is None:
        raise HTTPException(404, f"no snapshots for vendor '{slug}'")
    return out


def _workbook_grids():
    """The PRIMARY workbook only — the snapshot/cron path. Vendor snapshots and
    official numbers never read secondary sheets."""
    return mr_workbook.fetch_workbook(mr_config.SHEETS_SPREADSHEET_ID)


def _workbook_bundle(*, deep: bool, use_cache: bool = True):
    """(grids, profiles) across every connected workbook — the Ask/catalog
    substrate. Each workbook is profiled against its own cache; secondary tab
    titles are namespaced "<label> · <tab>" so answers cite which sheet, and a
    broken secondary never blocks the primary."""
    grids = _workbook_grids()
    profs = mr_profiles.profile_workbook(grids, year=mr_config.SHEETS_YEAR, deep=deep, use_cache=use_cache)
    for src in mr_sources_registry.extra_sources():
        try:
            egrids = mr_workbook.fetch_workbook(src["id"])
            eprofs = mr_profiles.profile_workbook(
                egrids, year=mr_config.SHEETS_YEAR, deep=deep, use_cache=use_cache, scope=src["id"])
        except Exception:
            logger.warning("MR secondary sheet %s unreadable; skipping", src["id"])
            continue
        label = str(src.get("label") or src["id"][:8])
        for g in egrids:
            g.title = f"{label} · {g.title}"
        for p in eprofs:
            p.title = f"{label} · {p.title}"
        grids.extend(egrids)
        profs.extend(eprofs)
    return grids, profs


@router.get("/mr/workbook")
def workbook_catalog(user=Depends(get_current_user)):
    """The agent's understanding of every tab (fast heuristic, or cached deep)."""
    try:
        _, profs = _workbook_bundle(deep=False)
    except Exception as exc:
        raise HTTPException(502, f"Could not read the spreadsheet: {exc}")
    return {"tabs": [asdict(p) for p in profs], "count": len(profs)}


@router.post("/mr/workbook/scan")
def workbook_scan(user=Depends(get_current_user),
                  act: Activity = trail.records("workbook_scan", "Deep-profiled the workbook")):
    """Deep-profile every tab with the LLM and cache the result."""
    try:
        _, profs = _workbook_bundle(deep=True, use_cache=False)
    except Exception as exc:
        raise HTTPException(502, f"Could not read the spreadsheet: {exc}")
    act.note(f"Deep-profiled {len(profs)} workbook tabs")
    return {"tabs": [asdict(p) for p in profs], "count": len(profs)}


#: Longest question ``/mr/ask`` accepts. A real one is a sentence; anything
#: past this is not a question, and it would be paid for twice.
ASK_QUESTION_LIMIT = 2_000


@router.post("/mr/ask")
def ask(body: dict | None = None, user=Depends(get_current_user),
        act: Activity = trail.records("ask", "Asked the researcher a question")):
    """Answer a natural-language question with grounded insight from the right tab(s)."""
    body = body or {}
    raw = body.get("question")
    # Only a string is a question. `str(...)` turned an explicit null into the
    # four-letter question "None", which passed the empty check and paid for a
    # tab-selection model call before Ask refused it for naming no period.
    question = raw.strip() if isinstance(raw, str) else ""
    if not question:
        raise HTTPException(400, "question is required")
    # The question is formatted into TWO provider prompts and echoed into the
    # payload, so an uncapped one is a signed-in caller's blank cheque.
    if len(question) > ASK_QUESTION_LIMIT:
        raise HTTPException(
            422, f"question is too long (limit {ASK_QUESTION_LIMIT:,} characters)")
    try:
        grids, profs = _workbook_bundle(deep=False)
    except Exception as exc:
        raise HTTPException(502, f"Could not read the spreadsheet: {exc}")
    grid_map = {g.title: g.rows for g in grids}
    # The dashboard's headline strip is the sheet's OWN Overall-tab figures
    # wherever it has them; Ask only had the grids, so the same question could
    # answer differently on the two surfaces. One scoped read of this
    # workspace's newest official-totals run closes that - deliberately not
    # `_load_dataset`, which rehydrates ~13 large documents for a question that
    # needs one. A store failure must not cost the answer: Ask degrades to the
    # tracker sums and says so in each fact's basis.
    try:
        official_totals = dict(_latest_official_run(_ws(user)).get("totals") or {})
    except runs.RunStoreError as exc:
        logger.warning("MR ask: official totals unreadable (%s); using tracker sums", exc)
        official_totals = {}
    answer = mr_insight.answer(
        question, profs, grid_map,
        timeframe=body.get("timeframe"), year=mr_config.SHEETS_YEAR,
        # A tab the workbook read cut short must not be totalled, and the
        # omission has to reach the answer payload rather than be discovered as
        # a short number. This map is the only place that knows.
        truncated=mr_workbook.truncation_map(grids),
        official_totals=official_totals,
    )
    act.note(f"Asked: {question}")
    return answer


def _may_remove_any_sheet(user: dict) -> bool:
    """Who may disconnect a sheet they did not connect.

    Admins and creators, and nobody else. They are also the only callers who
    can retire the pre-attribution rows — the ones connected before
    ``added_by`` existed, which no store can name an owner for.
    """
    return bool(user.get("is_admin") or user.get("is_creator"))


@router.get("/mr/sources")
def sheet_sources(user=Depends(get_current_user)):
    """Connected workbooks (multi-sheet). The primary tracker is always first.

    WORKSPACE_SHARED, deliberately: this lists what the whole workspace has
    connected, because the agent reads every one of them for every caller and
    reaches them through one shared service account rather than anybody's own
    Google identity. ``sources_registry``'s docstring carries the argument.

    Each row now says who connected it (``added_by``) and whether THIS caller
    may disconnect it (``can_remove``), so the console can hide a button that
    would only earn a 403.
    """
    return {
        "enabled": mr_sources_registry.multi_sheet_enabled(),
        "service_account": mr_sources_registry.service_account_email(),
        "sources": mr_sources_registry.list_sources(
            viewer_id=user["id"], privileged=_may_remove_any_sheet(user)),
    }


@router.post("/mr/sources")
def add_sheet_source(body: dict | None = None, user=Depends(get_current_user),
                     act: Activity = trail.records("connect_sheet", "Connected a sheet")):
    """Connect another Google Sheet by pasted link. Access is validated up
    front; the response carries the agent's first-pass read of every tab."""
    if not mr_sources_registry.multi_sheet_enabled():
        raise HTTPException(403, "Multi-sheet support is disabled on this deployment (MR_MULTI_SHEET).")
    body = body or {}
    sid = mr_sources_registry.parse_spreadsheet_id(str(body.get("url") or body.get("id") or ""))
    if not sid:
        raise HTTPException(400, "Paste a Google Sheets link (or its spreadsheet id).")
    sa = mr_sources_registry.service_account_email()
    try:
        meta = workbook_meta(sid)
    except Exception:
        raise HTTPException(
            403, f"Could not open that sheet. Share it with {sa} as Viewer, then try again.")
    try:
        src = mr_sources_registry.add_source(
            sid, label=str(meta.get("title") or sid), added_by=user["id"])
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    # First-pass understanding, cached per workbook — never fails the add.
    try:
        grids = mr_workbook.fetch_workbook(sid)
        profs = mr_profiles.profile_workbook(grids, year=mr_config.SHEETS_YEAR, deep=False, scope=sid)
        tabs = [asdict(p) for p in profs]
    except Exception:
        tabs = []
    act.note(f"Connected sheet “{src.get('label', sid)}”")
    return {"source": src, "tabs": tabs, "tab_count": len(meta.get("tabs") or [])}


@router.delete("/mr/sources/{spreadsheet_id}")
def remove_sheet_source(spreadsheet_id: str, user=Depends(get_current_user),
                        act: Activity = trail.records("disconnect_sheet",
                                                      "Disconnected a sheet", unit=CHANGE)):
    """Disconnect a secondary sheet — the agent stops reading it immediately.

    The registry is shared; this operation is not. Until 2026-09-05 it took no
    caller at all, so any signed-in user could permanently disconnect a sheet
    somebody else had connected. ``remove_source`` now requires the caller and
    refuses unless they connected it or hold an elevated role.

    A refusal is 403, not the 404 the run endpoints use. 404 exists there to
    avoid an ownership oracle — but this row is already in the caller's own
    ``GET /mr/sources`` listing, so a 404 would leak nothing and lie about a
    sheet the caller can plainly see. It also never reaches ``act.note``, so a
    refused attempt records no "disconnected" activity.
    """
    if not mr_sources_registry.multi_sheet_enabled():
        raise HTTPException(403, "Multi-sheet support is disabled on this deployment (MR_MULTI_SHEET).")
    try:
        removed = mr_sources_registry.remove_source(
            spreadsheet_id, requested_by=user["id"],
            privileged=_may_remove_any_sheet(user))
    except PermissionError as exc:
        raise HTTPException(403, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not removed:
        raise HTTPException(404, "source not found")
    act.note(f"Disconnected sheet {spreadsheet_id}")
    return {"removed": spreadsheet_id}


@router.get("/mr/connectors")
def connectors(user=Depends(get_current_user)):
    """Connection status for each platform the agent can pull from."""

    def env_status(var: str) -> str:
        return "connected" if os.environ.get(var) else "needs_setup"

    return [
        {"key": "google_sheets", "label": "Google Sheets", "logo": "google-sheets",
         "category": "Data source", "status": "connected",
         "detail": "Live performance tracker, pulled via the service account (viewer access)."},
        {"key": "google_drive", "label": "Google Drive", "logo": "google-drive",
         "category": "Data source", "status": "connected",
         "detail": "Read-only export access used to pull sheet tabs and the workbook."},
        {"key": "hubspot", "label": "HubSpot", "logo": "hubspot",
         "category": "CRM", "status": env_status("HUBSPOT_ACCESS_TOKEN"),
         "detail": "Lead-level demos & funnel data. Set HUBSPOT_ACCESS_TOKEN to enable live sync."},
        {"key": "google_ads", "label": "Google Ads", "logo": "google",
         "category": "Ads", "status": env_status("GOOGLE_ADS_DEVELOPER_TOKEN"),
         "detail": "Live campaign metrics. Set GOOGLE_ADS_DEVELOPER_TOKEN to enable."},
        {"key": "meta", "label": "META Ads", "logo": None,
         "category": "Ads", "status": env_status("META_ACCESS_TOKEN"),
         "detail": "Live campaign metrics. Set META_ACCESS_TOKEN to enable."},
        {"key": "csv", "label": "CSV / Excel upload", "logo": None,
         "category": "Manual", "status": "available",
         "detail": "Upload a platform export manually any time — no credentials needed."},
    ]


@router.get("/mr/targets")
def get_targets(user=Depends(get_current_user)):
    """This workspace's effective targets/thresholds (defaults merged with its
    own edits). Another workspace's edits are not visible here and never were
    meant to be — until 2026-08-21 they were, because this was one document for
    the whole deployment."""
    from marketing_research_agent import goals as mr_goals

    return mr_goals.get_targets(user["id"])


@router.post("/mr/targets")
def save_targets(body: dict | None = None, user=Depends(get_current_user),
                 act: Activity = trail.records("targets_saved", "Edited the targets",
                                               unit=CHANGE)):
    """Edit THIS workspace's targets/figures. Body:
    {"thresholds": {...}, "channel_goals": {"Google": {...}}}.
    Send {"reset": true} to return to the verbatim 2026 defaults."""
    from marketing_research_agent import goals as mr_goals

    body = body or {}
    if body.get("reset"):
        act.note("Targets reset to the defaults", action="targets_reset")
        return mr_goals.reset_targets(user["id"])
    try:
        saved = mr_goals.set_targets(user["id"], body)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    act.note(f"Targets saved — {', '.join(sorted(body)) or 'no fields'}")
    return saved


@router.get("/mr/config")
def get_config(user=Depends(get_current_user)):
    """Agent configuration: data source, report schedule, and thresholds."""
    from marketing_research_agent import goals as mr_goals

    _thr = mr_goals.thresholds(mr_goals.get_targets(user["id"]))
    return {
        "spreadsheet_id": mr_config.SHEETS_SPREADSHEET_ID,
        "spreadsheet_url": f"https://docs.google.com/spreadsheets/d/{mr_config.SHEETS_SPREADSHEET_ID}/edit",
        "year": mr_config.SHEETS_YEAR,
        "competitors": mr_config.COMPETITORS,
        "schedule": [
            {"report": "Daily Performance Summary", "cadence": "Daily · 3:00 PM PST"},
            {"report": "Weekly Performance Summary", "cadence": "Mondays · 12:00 PM PST"},
            {"report": "Monthly Performance Summary", "cadence": "1st of the month"},
            {"report": "Quarterly Performance Summary", "cadence": "Quarter start"},
            {"report": "Campaign Threshold Alert", "cadence": "Triggered"},
            {"report": "Competitor Change Digest", "cadence": "Weekly"},
            {"report": "Media Opportunity Report", "cadence": "Bi-weekly"},
            {"report": "UTM Attribution Summary", "cadence": "Weekly"},
            {"report": "ICP Audience Signal", "cadence": "Monthly"},
        ],
        # One resolve, five fields. This used to call ``thresholds()`` once per
        # field — five reads of the same document to build one dict.
        "thresholds": {k: (int(_thr[k] * 100) if k == "conversion_drop_pct" else _thr[k])
                       for k in ("cost_per_booking_flag", "cac_red",
                                 "cost_per_qualified_lead_red", "spend_no_demo_limit",
                                 "conversion_drop_pct")},
    }


@router.get("/mr/report-periods")
def report_periods(user=Depends(get_current_user)):
    """Months and quarters that actually hold tracker data — feeds the Reports
    panel's month/quarter picker. Workspace-wide: it is the same data every
    member's Overview is drawn from."""
    return reports.available_periods(_load_dataset(_ws(user)))


@router.post("/mr/reports/{kind}")
def make_report(kind: str, body: dict | None = None, user=Depends(get_current_user),
                act: Activity = trail.records("report", "Built a report")):
    """Build one report. Body (optional): ``{"period": "2026-07" | "2026-Q3"}`` —
    monthly/quarterly only. An explicit period never substitutes another month's
    data: an empty window is a 422, not a silent fallback.

    The figures come from the WORKSPACE's dataset (``_ws``); the report itself is
    the caller's own — stamped with, and judged against the targets of,
    ``user["id"]`` — and stays private to them."""
    if kind not in reports.KINDS:
        raise HTTPException(404, f"unknown report kind '{kind}' (expected one of {reports.KINDS})")
    if kind in reports.BOARD_KINDS:
        # The board kinds are real kinds - they list and read back here like any
        # other - but they take two periods, so they cannot come in through this
        # route's one-``period`` body. Say where they do rather than 500 on a
        # builder that refuses them.
        raise HTTPException(422, f"'{kind}' is built at POST /api/mr/board-report.")
    period = str((body or {}).get("period") or "").strip() or None
    if period and kind not in ("monthly_summary", "quarterly_summary"):
        raise HTTPException(422, f"'{kind}' reports don't take a period.")
    if kind == "daily_movement":
        report = reports.build(kind, {"snapshot_deltas": mr_snapshots.deltas_for()}, user_id=user["id"])
    else:
        try:
            report = reports.build(kind, _load_dataset(_ws(user)), user_id=user["id"],
                                   period=period)
        except reports.PeriodError as exc:
            raise HTTPException(422, str(exc))
    act.note(f"Built {kind} report" + (f" for {period}" if period else ""),
             action=f"report:{kind}", run_id=str(report.get("id") or "") or None)
    return report


def _period_field(body: dict | None, name: str) -> str:
    """One period out of the request body, as text or not at all.

    ``str()`` on whatever arrived is what let ``{"period": {"x": "..."}}`` reach
    the 422 body as the structure's own repr. A period the caller did not send
    as a string is not a malformed period, it is an absent one, and it gets the
    same "needs a 'period'" answer an empty body gets. Length is bounded
    further down, in ``reports._echo_period``.
    """
    value = (body or {}).get(name)
    return value.strip() if isinstance(value, str) else ""


@router.post("/mr/board-report")
def board_report(body: dict | None = None, user=Depends(get_current_user),
                 act: Activity = trail.records("report:board",
                                               "Built a board report")):
    """The board comparison as DATA - the ledger, as JSON.

    Body: ``{"period": "2026-Q2"}`` for one column, plus ``"compare_to":
    "2026-Q1"`` for the two-column comparison. A period is ``YYYY-MM``,
    ``YYYY-Qn`` or ``YYYY``, and the two columns need not be the same shape.

    Deliberately no HTML and no PDF. The renderer is a separate module; the
    route that returns a document lands with it, and until then this returns the
    ledger and nothing pretends to be a document.

    **Dark by default.** With ``MR_BOARD_REPORT`` unset this answers 404 with
    the same ``"Not Found"`` detail an unrouted path gives, so a deployment
    that has not enabled the feature builds no report and writes no run. It
    does not hide that the route is registered: FastAPI parses the body before
    it resolves dependencies, so a malformed body answers 422 here and 404 on
    a path that does not exist. This is a kill switch, not concealment.

    The auth ordering is the load-bearing part, and it holds: the switch sits
    INSIDE the handler, after ``Depends``, so an anonymous caller gets 401
    whether the feature is on or off and never learns which it was.

    Idempotent: the report is keyed on (periods, capture date, generator
    version), so asking twice for the same quarter of the same capture returns
    the run already stored rather than deriving it again. ``reused`` on the
    response says which happened.

    **Whole workspace.** Unlike the campaign reports, a board report is stamped
    with, read from and served under the WORKSPACE key (``_ws``): visibility was
    locked "whole workspace" on 2026-09-05, and it is a pure function of the
    shared roll-up, so a colleague asking for the same quarter of the same
    capture is handed the run already stored (``reused: true``) instead of a
    second copy. The idempotency lookup is still scoped to that one key at the
    query, so a run stamped with any other key can never be served from here.
    """
    if not reports.board_report_enabled():
        raise HTTPException(404, "Not Found")
    period = _period_field(body, "period")
    compare_to = _period_field(body, "compare_to") or None
    if not period:
        raise HTTPException(
            422, "A board report needs a 'period' (YYYY-MM, YYYY-Qn or YYYY).")
    ws = _ws(user)
    try:
        report = reports.build_board_report(
            _load_dataset(ws), user_id=ws,
            period=period, compare_to=compare_to)
    except reports.PeriodError as exc:
        raise HTTPException(422, str(exc))
    # The coverage line goes in the activity trail on purpose: "24 of 38 filled"
    # is how anyone reading the trail later tells a thin CAPTURE from a thin
    # quarter, and it is the number that moves when a pull lands.
    cov = [f"{c['filled_count']}/{c['metric_count']}"
           for c in ((report.get("structured") or {}).get("coverage") or {}).get("columns", [])]
    act.note(f"Built the board report for {period}"
             + (f" vs {compare_to}" if compare_to else "")
             + (f" ({', '.join(cov)} metrics filled)" if cov else "")
             + (" - served from the store" if report.get("reused") else ""),
             action="report:board_report", run_id=str(report.get("id") or "") or None)
    return report


def _listed_period(structured: dict) -> str | None:
    """The Period column for one stored run, whatever kind wrote it.

    The campaign kinds carry one window as ``period.label``. The board kinds
    carry ``periods`` instead - one entry, or two, because a board report is a
    comparison of two windows and one label cannot say so. Reading only the
    first shape listed every board run with an em-dash under Period, and in this
    UI an em-dash means "not reported": the absent-vs-unknown confusion the board
    report exists to refuse, showing up in the list of board reports.

    Derived here rather than written into the stored run: the runs are already
    persisted, so a stored field would only be true for the ones built after it,
    and the label is a presentation of ``periods``, not a new fact about them.
    """
    single = (structured.get("period") or {}).get("label")
    if single:
        return str(single)
    labels = [str(p.get("label") or p.get("key") or "").strip()
              for p in (structured.get("periods") or [])
              if isinstance(p, dict)]
    labels = [label for label in labels if label]
    if not labels:
        return None
    # Both windows, in the order the document prints them. Deduplicated because
    # a ledger CAN name the same period twice (``single_period`` is a period
    # against itself) and "Q1 vs Q1" would read as a comparison nobody asked for.
    return " vs ".join(dict.fromkeys(labels))


def _may_read_run(run: dict, user: dict) -> bool:
    """Whether this caller may read this stored report run.

    Reports are private to whoever built them: the run's ``user_id`` is the
    caller's own. The one exception is a BOARD run, which lives under the
    WORKSPACE key and is the workspace's to read. Everything else — another
    member's daily summary, a run stamped with a key that is neither the
    caller's nor their workspace's — is not theirs, and the routes answer 404
    for it, never 403 (a 403 would confirm the id exists).

    Unshared, the workspace key IS the caller's id, so this collapses to the
    plain ownership check these routes always had. The comparison is raw, not
    ``str()``: MR treats ``7`` and ``"7"`` as two tenants and the type contract
    suite pins it.
    """
    owner = run.get("user_id")
    if owner == user["id"]:
        return True
    return run.get("kind") in reports.BOARD_KINDS and owner == _ws(user)


@router.get("/mr/runs")
def list_report_runs(user=Depends(get_current_user)):
    """The caller's saved reports: their own runs, plus the workspace's board
    reports. Unshared that is ONE query; shared it is two equality queries — the
    caller's own kinds under their id, the board kinds under the workspace key —
    merged newest first."""
    ws = _ws(user)
    kinds = tuple(reports.KINDS)
    if ws == user["id"]:
        rows = runs.list_runs(user["id"], kind=kinds)
    else:
        rows = (runs.list_runs(user["id"], kind=kinds)
                + runs.list_runs(ws, kind=tuple(reports.BOARD_KINDS)))
        rows.sort(key=lambda r: r.get("generated_at") or "", reverse=True)
    return [
        {"id": r["id"], "kind": r.get("kind"), "generated_at": r.get("generated_at"),
         "period": _listed_period(r.get("structured") or {})}
        for r in rows
    ]


@router.get("/mr/runs/{run_id}")
def get_report_run(run_id: str, user=Depends(get_current_user)):
    """One saved REPORT, whole. Only the report kinds are served here — the same
    filter ``report_run_pdf`` has. ``mr_runs`` also holds the workbook-derived
    kinds (``dataset`` with its metrics and leads, ``official_spend``,
    ``lead_analysis``), and under the shared workspace they are stamped with the
    workspace key: the one account whose id EQUALS that key satisfies
    ``_may_read_run``'s own-id clause for every one of them. Without this filter
    it could open, by run id, documents no route is meant to hand over whole."""
    run = runs.get_run(run_id)
    if not run or run.get("kind") not in reports.KINDS or not _may_read_run(run, user):
        raise HTTPException(404, "run not found")
    return run


def _pdf_response(data: bytes, filename: str) -> StreamingResponse:
    # Streamed (no Content-Length) so Cloud Run's 32 MiB buffered-response cap
    # never bites — same pattern as the Creative Agent's artifact download.
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/mr/runs/{run_id}/pdf")
def report_run_pdf(run_id: str, user=Depends(get_current_user),
                   act: Activity = trail.records("export:report_pdf",
                                                 "Downloaded a report PDF")):
    """The Reports panel document as a PDF — same sections, same order, same
    figures the user is looking at on screen."""
    run = runs.get_run(run_id)
    if not run or not _may_read_run(run, user) or run.get("kind") not in reports.KINDS:
        raise HTTPException(404, "run not found")
    if run.get("kind") in reports.BOARD_KINDS:
        # ``mr_pdf.report_pdf`` renders the campaign report's sections from a
        # narrative this kind does not have. A board PDF is a different document
        # with its own renderer, and now has its own route - name it, the way
        # ``make_report`` names where a board report is built.
        raise HTTPException(
            404, "a board report's PDF is at "
                 f"GET /api/mr/board-report/{run_id}/pdf, not here.")
    stamp = str(run.get("generated_at", ""))[:10] or date.today().isoformat()
    act.note(f"Downloaded the {run['kind']} report as PDF ({stamp})", run_id=run_id)
    return _pdf_response(mr_pdf.report_pdf(run), f"mr-{run['kind']}-{stamp}.pdf")


# --------------------------------------------------------------------------- #
# The board report as a DOCUMENT - the HTML, and the PDF rendered from it
# --------------------------------------------------------------------------- #
# ``POST /mr/board-report`` persists the ledger as JSON and deliberately returns
# no document. These two routes ARE the document, and they read the stored run
# rather than re-deriving it. Re-deriving would pick up whatever capture has
# landed since, so the PDF a client is emailed could quietly disagree with the
# JSON the panel is showing. One stored run in, one page out.
#
# Both sit behind the same kill switch as the POST, and the switch sits INSIDE
# the handler, after ``Depends(get_current_user)``, for the reason spelled out
# there: an anonymous caller gets 401 whether the feature is on or off and never
# learns which it was.


#: Whose report this is. ``board_report_render.render`` prints "Brand not set"
#: when nobody names the brand - the right default for a library function whose
#: caller might forget, and the wrong thing to email a client. So the call site
#: names it. This deployment is one shared workspace reading one tracker
#: (``mr_config.SHEETS_SPREADSHEET_ID`` defaults to Legal Soft's workbook), so
#: there is one brand today; ``MR_BOARD_REPORT_BRAND`` moves it without a
#: redeploy, and the workspace record supplies it when the workspace boundary
#: lands.
_DEFAULT_BOARD_BRAND = "Legal Soft"


def _board_brand() -> str:
    return os.environ.get("MR_BOARD_REPORT_BRAND", "").strip() or _DEFAULT_BOARD_BRAND


def _board_run(run_id: str, user: dict) -> dict:
    """A board run this caller may read - their workspace's, or one they built
    themselves before board runs moved to the workspace key - or 404.

    The same check as ``get_report_run`` and ``report_run_pdf`` above
    (:func:`_may_read_run`), narrowed to the board kinds, and the same answer for
    the same reason: a run another workspace owns is *not found*, never 403 - a
    403 would confirm the id exists.
    """
    run = runs.get_run(run_id)
    if (not run or run.get("kind") not in reports.BOARD_KINDS
            or not _may_read_run(run, user)):
        raise HTTPException(404, "board report not found")
    return run


def _untracked_of(stored: dict | None):
    from marketing_research_agent import board_report as br

    if not stored:
        return None
    return br.Untracked(**{k: stored.get(k) for k in
                           ("spend", "spend_pct", "revenue", "revenue_pct",
                            "clients", "clients_pct")})


def _month_cell_of(stored: dict):
    from marketing_research_agent import board_report as br

    return br.MonthCell(month=stored["month"], values=dict(stored.get("values") or {}))


def _channel_totals_of(stored: dict):
    """A stored ``ChannelTotals.as_dict()`` back as the dataclass.

    ``roas_pct`` and ``cac`` are recomputed properties, not fields, so they read
    back from ``spend``/``revenue``/``clients`` exactly as they were written.
    """
    from marketing_research_agent import board_report as br

    return br.ChannelTotals(channel=stored["channel"], spend=stored.get("spend"),
                            revenue=stored.get("revenue"), clients=stored.get("clients"))


def _channel_side_of(stored: dict, suffix: str):
    """One side of a stored ``ChannelRow.as_dict()``.

    That shape is FLATTENED - ``spend_a``/``revenue_a``/``roas_a``/``cac_a`` per
    side - and it drops ``clients``, which ``cac`` is a property of. ``clients``
    is therefore recovered as ``spend / cac``, and that is exact for everything
    this branch renders: ``cac`` is stored already rounded to 2dp, so
    ``round(spend / (spend / cac), 2) == cac``, and ``clients`` itself is only
    ever printed by the ONE-period channel table, which is stored unflattened
    (``_channel_totals_of``) and never reaches here. ``roas_pct`` recomputes from
    spend and revenue, both of which survive the flattening intact.

    All four fields absent means the channel had no figures on this side at all,
    which is an absent side and not a zeroed one.
    """
    from marketing_research_agent import board_report as br

    got = {f: stored.get(f"{f}_{suffix}") for f in ("spend", "revenue", "roas", "cac")}
    if all(v is None for v in got.values()):
        return None
    spend, cac = got["spend"], got["cac"]
    return br.ChannelTotals(
        channel=stored["channel"], spend=spend, revenue=got["revenue"],
        clients=(spend / cac) if (spend is not None and cac) else None)


def _board_ledger(structured: dict):
    """A stored run's ``structured`` block back as a ``ReportLedger``.

    The renderer takes the dataclass; the store holds its ``as_dict()``; nothing
    in the agent walks back the other way. It lives here, at the one call site
    that needs it, rather than as a new seam in modules another change is in
    flight on.

    Two stored shapes, because ``reports.build_board_report`` writes two:

    * the comparison is ``ReportLedger.as_dict()`` verbatim - rebuilt field for
      field, including the "Other / untracked" row ``compare()`` already
      appended to ``channels`` (so this must NOT re-run ``compare``, which would
      append it a second time);
    * the one-period report is ``reports._single_column`` - a ``PeriodRollup``
      flattened, with ONE ``columns`` entry and rows carrying ``value`` instead
      of ``a``/``b``. It is rebuilt as the rollup it came from and handed to
      ``single_period()``, which is exactly what the POST path built.

    Raises ``ValueError`` with a caller-safe message when the run was written by
    a different generator; anything else structural comes out as the raw
    ``KeyError``/``TypeError``/``IndexError`` and is logged, not echoed.
    """
    from marketing_research_agent import board_report as br
    from marketing_research_agent import board_report_render as br_render

    generator = str(structured.get("generator") or "")
    if generator != br.GENERATOR_VERSION:
        raise ValueError(
            f"this run was written by generator '{generator or 'unknown'}' and this "
            f"service renders '{br.GENERATOR_VERSION}' - rebuild it at "
            "POST /api/mr/board-report")

    periods = [br.PeriodSpec(key=p["key"], label=p["label"], months=tuple(p["months"]))
               for p in structured["periods"]]
    columns = list(structured["columns"])
    channels = list(structured.get("channels") or [])
    untracked = list(structured.get("untracked") or [])
    monthly = list(structured.get("monthly") or [])
    gaps = tuple(structured.get("gaps") or ())

    if len(columns) == 1:
        rollup = br.PeriodRollup(
            period=periods[0],
            components=dict(structured.get("components") or {}),
            # Absent is not zero, one level down too: a metric the period could
            # not fill is simply not a key, which is what ``PeriodRollup.value``
            # reads and what makes the em-dash rather than a $0.
            values={r["key"]: r["value"] for r in structured["rows"]
                    if r.get("value") is not None},
            channels=tuple(_channel_totals_of(c) for c in channels),
            untracked=_untracked_of(untracked[0] if untracked else None),
            gaps=gaps,
            monthly=tuple(_month_cell_of(c) for c in monthly),
        )
        return br_render.single_period(rollup)

    sides = (untracked + [None, None])[:2]
    return br.ReportLedger(
        columns=(columns[0], columns[1]),
        periods=(periods[0], periods[1]),
        rows=tuple(br.LedgerRow(key=r["key"], label=r["label"], group=r["group"],
                                format=r["format"], polarity=r["polarity"],
                                a=r.get("a"), b=r.get("b"), basis=r.get("basis"))
                   for r in structured["rows"]),
        channels=tuple(br.ChannelRow(c["channel"], _channel_side_of(c, "a"),
                                     _channel_side_of(c, "b"))
                       for c in channels),
        untracked=(_untracked_of(sides[0]), _untracked_of(sides[1])),
        gaps=gaps,
        cache_key=str(structured.get("cache_key") or ""),
        monthly=tuple(tuple(_month_cell_of(c) for c in side)
                      for side in (monthly + [[], []])[:2]),
    )


def _board_document(run: dict) -> str:
    """The run as one self-contained HTML document, or a 422 that says why not.

    ``board_report_render`` is imported HERE and not at module scope on purpose:
    it carries the embedded font faces, which every other route in this file
    would otherwise pay to import, and a fault in that generated module must not
    take the whole router down at import time.
    """
    from marketing_research_agent import board_report_render as br_render

    structured = run.get("structured") or {}
    try:
        ledger = _board_ledger(structured)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    except (KeyError, TypeError, IndexError):
        # The shape is ours, not the caller's, so the caller gets the action and
        # the log gets the field. Echoing the KeyError would publish the store's
        # internal schema to anyone who can reach the route.
        logger.exception("mr board report %s: stored ledger could not be rebuilt",
                         run.get("id"))
        raise HTTPException(
            422, "this board run's stored ledger cannot be rendered by this service "
                 "- rebuild it at POST /api/mr/board-report")

    captured = str(structured.get("captured_on") or "")
    try:
        captured_on = date.fromisoformat(captured) if captured else None
    except ValueError:
        # The footer's date line is not worth a 500. A run with an unparseable
        # capture stamp renders without it rather than not at all.
        logger.warning("mr board report %s: unparseable captured_on %r",
                       run.get("id"), captured)
        captured_on = None
    return br_render.render(ledger, brand=_board_brand(), captured_on=captured_on)


def _board_stamp(run: dict) -> str:
    return str(run.get("generated_at", ""))[:10] or date.today().isoformat()


@router.get("/mr/board-report/{run_id}/html")
@silent("an inline preview the console re-renders on every view of the report - "
        "the PDF beside it is the recorded unit, and a row per view would grow "
        "the trail faster than the reports it describes")
def board_report_html(run_id: str, user=Depends(get_current_user)):
    """One stored board run as a complete, self-contained HTML document.

    No script, no stylesheet link, no remote image and no ``http`` of any kind -
    the charts are inline SVG and the fonts are embedded, so the file survives
    being emailed to a client behind a corporate proxy. Served ``inline`` so the
    console can preview it, and ``nosniff`` so nothing renders it as anything
    else.
    """
    if not reports.board_report_enabled():
        raise HTTPException(404, "Not Found")
    run = _board_run(run_id, user)
    html = _board_document(run)
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={
            "Content-Disposition":
                f'inline; filename="mr-board-report-{_board_stamp(run)}.html"',
            "X-Content-Type-Options": "nosniff",
        },
    )


# --- the PDF renderer, which is another service ------------------------------
# Every call below is treated as hostile: an explicit timeout, a retry policy
# that only retries what a retry can fix, and one defined answer per failure.
# There is no fallback path and there must never be one. ``pdf_export.py``
# renders a DIFFERENT report in a different visual identity, and an HTML file
# under a ``.pdf`` name is not a PDF - a missing renderer is a loud failure, not
# a substitution.

#: The request contract ``renderer/src/app.js`` checks (its ``v`` field).
_RENDERER_CONTRACT_VERSION = 1

#: Attempts for one document. The render is a pure function of the HTML and the
#: renderer holds no state, so a retry is safe. It is also expensive - a
#: Chromium page load - so only the failures a retry can actually fix are
#: retried: a refused or dropped connection, and the 502/504 a Cloud Run
#: instance answers while it is still coming up at ``min-instances=0``.
#:
#: NOT retried: a read timeout (the same render times out again and doubles the
#: caller's wait), any 4xx, and 503 - the renderer uses 503 to mean "fail closed,
#: my RENDERER_TOKEN is unset", and hammering a deliberate refusal is noise.
_RENDERER_ATTEMPTS = 2
_RENDERER_BACKOFF_SECONDS = 1.0
_RENDERER_RETRY_STATUS = frozenset({502, 504})


def _renderer_read_timeout() -> float:
    """Seconds to wait for the render itself.

    Generous on purpose. The renderer's own budget is ``PDF_TIMEOUT_MS`` per
    phase (60s laying the page out, 60s writing the PDF) on top of a cold start
    that launches Chromium, so a short read timeout turns a slow-but-working
    render into a 504 nobody can act on.
    """
    try:
        return max(1.0, float(os.environ.get("RENDERER_TIMEOUT_SECONDS", "150")))
    except ValueError:
        return 150.0


def _renderer_config() -> tuple[str, str]:
    """``(url, token)`` for the PDF renderer, or a 503 naming what is unset.

    By env var NAME, never by value - here, in the error the caller reads, and
    in every log line below.
    """
    url = os.environ.get("RENDERER_URL", "").strip().rstrip("/")
    token = os.environ.get("RENDERER_TOKEN", "").strip()
    missing = [name for name, value in (("RENDERER_URL", url), ("RENDERER_TOKEN", token))
               if not value]
    if missing:
        raise HTTPException(
            503,
            "PDF rendering is not configured on this service: "
            + " and ".join(missing)
            + (" is unset." if len(missing) == 1 else " are unset.")
            + " The board report is available as HTML at "
              "GET /api/mr/board-report/{run_id}/html.")
    return url, token


def _render_pdf_via_service(html: str, *, run_id: str) -> tuple[bytes, str]:
    """The HTML rendered to PDF bytes by the renderer service.

    Returns ``(pdf_bytes, blocked_subresource_count)``. Raises ``HTTPException``
    - never returns a substitute - on every failure path.
    """
    url, token = _renderer_config()
    endpoint = f"{url}/pdf"
    payload = {"v": _RENDERER_CONTRACT_VERSION, "html": html}
    # The shared secret goes in this header and nowhere else: not a query
    # string, not a log line, not an error body.
    headers = {"X-Renderer-Token": token}
    timeout = httpx.Timeout(_renderer_read_timeout(), connect=10.0)

    for attempt in range(1, _RENDERER_ATTEMPTS + 1):
        unreachable: str | None = None
        try:
            resp = httpx.post(endpoint, json=payload, headers=headers, timeout=timeout)
        except (httpx.ConnectError, httpx.ConnectTimeout,
                httpx.RemoteProtocolError) as exc:
            unreachable = type(exc).__name__
            logger.warning("mr board pdf %s: attempt %d/%d could not reach the "
                           "renderer (RENDERER_URL) - %s",
                           run_id, attempt, _RENDERER_ATTEMPTS, exc)
            if attempt >= _RENDERER_ATTEMPTS:
                raise HTTPException(
                    502, f"could not reach the PDF renderer ({unreachable}) after "
                         f"{_RENDERER_ATTEMPTS} attempts - RENDERER_URL points at a "
                         "service this deployment cannot connect to.")
        except httpx.TimeoutException as exc:
            logger.error("mr board pdf %s: the renderer did not answer in %.0fs - %s",
                         run_id, _renderer_read_timeout(), exc)
            raise HTTPException(
                504, "the PDF renderer did not answer within "
                     f"{_renderer_read_timeout():.0f}s (RENDERER_TIMEOUT_SECONDS). "
                     "The report was not rendered.")
        except httpx.HTTPError as exc:
            logger.error("mr board pdf %s: renderer transport failure - %s", run_id, exc)
            raise HTTPException(
                502, f"the PDF renderer call failed ({type(exc).__name__}).")

        if unreachable is not None:
            time.sleep(_RENDERER_BACKOFF_SECONDS * attempt)
            continue

        if resp.status_code == 200:
            body = resp.content
            if not body.startswith(b"%PDF"):
                # 200 carrying something that is not a PDF is exactly the shape
                # of a fake success - a proxy's error page streamed under
                # application/pdf. Refuse it rather than hand it over.
                logger.error("mr board pdf %s: renderer answered 200 with %d bytes that "
                             "are not a PDF", run_id, len(body))
                raise HTTPException(
                    502, "the PDF renderer answered 200 with something that is not a "
                         "PDF; nothing was rendered.")
            return body, resp.headers.get("x-blocked-subresources", "0")

        if resp.status_code == 401:
            logger.error("mr board pdf %s: the renderer rejected this service's "
                         "RENDERER_TOKEN", run_id)
            raise HTTPException(
                502, "the PDF renderer rejected this service's credentials - the "
                     "RENDERER_TOKEN here does not match the renderer's.")
        if resp.status_code == 503:
            logger.error("mr board pdf %s: the renderer answered 503 (its own "
                         "RENDERER_TOKEN is unset, or it is unavailable)", run_id)
            raise HTTPException(
                503, "the PDF renderer is unavailable: it answered 503, which is what "
                     "it returns when its own RENDERER_TOKEN is unset.")
        if resp.status_code in _RENDERER_RETRY_STATUS and attempt < _RENDERER_ATTEMPTS:
            logger.warning("mr board pdf %s: renderer answered %d on attempt %d/%d",
                           run_id, resp.status_code, attempt, _RENDERER_ATTEMPTS)
            time.sleep(_RENDERER_BACKOFF_SECONDS * attempt)
            continue

        logger.error("mr board pdf %s: renderer answered %d", run_id, resp.status_code)
        raise HTTPException(
            502, f"the PDF renderer answered {resp.status_code}; the report was not "
                 "rendered.")

    # Unreachable: the loop either returns or raises on its last attempt.
    raise HTTPException(502, "the PDF renderer could not be reached.")


@router.get("/mr/board-report/{run_id}/pdf")
def board_report_pdf(run_id: str, user=Depends(get_current_user),
                     act: Activity = trail.records("export:board_pdf",
                                                   "Downloaded a board report PDF")):
    """One stored board run as a PDF - the same document as ``/html``, laid out
    by the renderer service's headless Chromium.

    There is no local fallback. With ``RENDERER_URL`` or ``RENDERER_TOKEN``
    unset this answers 503 naming the variable; if the renderer is unreachable,
    slow or unhappy it answers 502/504 saying so. It never returns the reportlab
    export (a different report in a different visual identity), and never HTML
    under a ``.pdf`` name.
    """
    if not reports.board_report_enabled():
        raise HTTPException(404, "Not Found")
    run = _board_run(run_id, user)
    html = _board_document(run)
    pdf, blocked = _render_pdf_via_service(html, run_id=run_id)
    if blocked not in ("", "0"):
        # The document's whole contract is that it is self-contained. A non-zero
        # count means it grew an external dependency the renderer refused to
        # fetch, so the PDF is missing something. Loud in the log and stated on
        # the response rather than swallowed.
        logger.error("mr board pdf %s: the renderer blocked %s subresource(s) - the "
                     "board document is no longer self-contained", run_id, blocked)
    stamp = _board_stamp(run)
    act.note(f"Downloaded the {run['kind']} as PDF ({stamp})", run_id=run_id)
    response = _pdf_response(pdf, f"mr-board-report-{stamp}.pdf")
    response.headers["X-Blocked-Subresources"] = blocked
    return response


@router.get("/mr/lead-analysis/pdf")
def lead_analysis_pdf(month: str | None = None, user=Depends(get_current_user),
                      act: Activity = trail.records("export:leads_pdf",
                                                    "Downloaded the leads PDF")):
    """The Leads panel as a PDF — same story line, red-flag card and vendor
    table the user is looking at. ``month`` (YYYY-MM) defaults to the latest;
    an unknown month is a 422, never a silently substituted one."""
    run = _latest_lead_run(_ws(user))
    if not run or not (run.get("summary") or {}).get("months"):
        raise HTTPException(404, "no lead-analysis data yet")
    try:
        data = mr_pdf.leads_pdf(run, month=month)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    label = month or (run.get("summary") or {}).get("latest_month") or "latest"
    act.note(f"Downloaded the lead analysis for {label} as PDF")
    return _pdf_response(data, f"mr-leads-{label}.pdf")


@router.get("/mr/snapshots/vendor/{slug}/pdf")
def snapshots_vendor_pdf(slug: str, date_iso: str | None = None,
                         user=Depends(get_current_user),
                         act: Activity = trail.records("export:vendor_pdf",
                                                       "Downloaded a vendor dossier")):
    """The Vendors panel dossier as a PDF — official summary, day movement and
    the full section dossier exactly as rendered on screen."""
    detail = mr_snapshots.vendor_detail(slug, date_iso)
    if detail is None:
        raise HTTPException(404, f"no snapshots for vendor '{slug}'")
    benchmarks = None
    try:
        benchmarks = (mr_snapshots.portfolio() or {}).get("benchmarks")
    except Exception:  # benchmarks only tint cells; the dossier must still export
        pass
    snap_date = (detail.get("snapshot") or {}).get("date") or date.today().isoformat()
    act.note(f"Downloaded the {slug} dossier for {snap_date} as PDF")
    return _pdf_response(mr_pdf.vendor_pdf(detail, benchmarks),
                         f"mr-vendor-{slug}-{snap_date}.pdf")


@router.post("/mr/schedule/{period}")
def trigger_schedule(period: str, user=Depends(get_current_user),
                     act: Activity = trail.records("schedule", "Ran a schedule")):
    fn = {
        "daily": schedule.run_daily,
        "weekly": schedule.run_weekly,
        "biweekly": schedule.run_biweekly,
        "monthly": schedule.run_monthly,
    }.get(period)
    if not fn:
        raise HTTPException(404, f"unknown period '{period}'")
    # The workspace's figures, the caller's own reports and red lines.
    result = fn(_load_dataset(_ws(user)), user_id=user["id"])
    act.note(f"Ran the {period} schedule", action=f"schedule:{period}")
    return result
