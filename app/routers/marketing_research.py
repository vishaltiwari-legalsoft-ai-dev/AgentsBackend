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
import os
import tempfile
import threading
from datetime import date, datetime, timezone

from fastapi import (
    APIRouter, Depends, File, Form, HTTPException, Request, Response, UploadFile,
)
from fastapi.responses import StreamingResponse

from app.security import get_current_user
from app.services import run_tracking

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
from marketing_research_agent.config import COLUMN_MAPS
from marketing_research_agent.schemas import CampaignMetric, DateRange, Lead
from marketing_research_agent.sources.csv_source import CsvSource
from marketing_research_agent.sources.sheets_source import (
    SheetsSource,
    fetch_all_trackers,
    fetch_official_totals,
    fetch_tab_values,
    is_rollup_platform,
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


# 207 (RFC 4918 Multi-Status) = some of it worked. Deliberately still 2xx: a
# partially-degraded pull must not make Cloud Scheduler retry a permanently bad
# tab forever, so it stays 2xx and shouts in the log instead. Total failure is a
# 5xx (below), which the scheduler does retry and alert on.
_PULL_HTTP_STATUS = {"ok": 200, "partial": 207}

# Per-workspace overlap guard. Two pulls interleaving their write and delete
# passes is a data-loss race; this is a single-process guard (Cloud Run runs
# several instances), which closes the common case — cron firing while the user
# hits "Pull" — but is not a distributed lease.
_PULL_LOCKS: dict[str, threading.Lock] = {}
_PULL_LOCKS_GUARD = threading.Lock()


def _pull_lock(user_id: str) -> threading.Lock:
    with _PULL_LOCKS_GUARD:
        return _PULL_LOCKS.setdefault(user_id, threading.Lock())


def _track(user: dict, action: str, task: str, *, usage_action: str = "generate") -> None:
    """Mandatory usage trail → agent_runs__a6 + master runs (admin DB panel)."""
    run_tracking.record_activity(
        user, agent_id=MR_AGENT_ID, agent_name=MR_AGENT_NAME, category="data",
        action=action, task=task, usage_action=usage_action,
    )


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
    summary = mr_leads.summarize(records, tracker_rollups=_tracker_rollups_by_month(user_id))
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
    """Reassemble the user's ingested data into one dataset. Keeps a per-vendor
    view (one entry per source tab/upload) so reports can name vendors.

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
            "lead_summary": (lead_run or {}).get("summary"),
            "today": date.today(), "sources": sources}


@router.post("/mr/ingest")
async def ingest(
    file: UploadFile = File(...),
    platform: str = Form(...),
    user=Depends(get_current_user),
):
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
        "user_id": user["id"],
        "agent_id": MR_AGENT_ID,
        "platform": platform,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "metrics": [m.__dict__ for m in metrics],
        "leads": [l.__dict__ for l in leads],
        "gaps": [g.__dict__ for g in (m_gaps + l_gaps)],
    }
    runs.save_run(run)
    _track(user, "ingest",
           f"Uploaded {platform} export — {len(metrics)} metrics, {len(leads)} leads")
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
):
    """Pull the live Google-Sheets performance tracker into datasets.

    Body (all optional): ``{"gid": "...", "brand": "...", "year": 2026}``.
    With a ``gid`` → that single tab is pulled (fast CSV export). With no gid →
    the whole workbook is scanned and every performance-tracker tab is ingested
    (auto-discovery; non-tracker tabs are skipped). Each tab becomes one dataset
    run of channel-aggregate monthly metrics.

    Status is honest: 200 clean, 207 some component degraded (details in
    ``degraded``), 502 the pull failed and NOTHING was changed, 409 another
    pull for this workspace is mid-flight."""
    body = body or {}
    year = int(body.get("year") or mr_config.SHEETS_YEAR)

    if body.get("gid"):
        src = SheetsSource(
            mr_config.SHEETS_SPREADSHEET_ID, str(body["gid"]), year=year, brand=body.get("brand")
        )
        try:
            metrics, gaps = src.fetch_campaign_metrics(_FULL_RANGE)
        except Exception as exc:  # auth/network/format — honest 502, nothing written
            logger.warning("MR single-tab pull failed for gid %s: %s", body["gid"], exc)
            raise HTTPException(502, f"Could not pull tab {body['gid']}: {exc}") from exc
        row, _durable = _persist_sheet_dataset(user["id"], str(body["gid"]), metrics, gaps)
        result = {"tabs": [row], "status": "ok", "ingested": 1, "failed": 0, "degraded": []}
    else:
        try:
            result = _ingest_sheet_all(user["id"], year)
        except SheetPullBusy as exc:
            raise HTTPException(409, str(exc)) from exc
        except SheetPullError as exc:
            raise HTTPException(
                502, f"Sheet pull failed — your existing data was left untouched. {exc}"
            ) from exc

    response.status_code = _PULL_HTTP_STATUS[result["status"]]
    ok = len(result["tabs"]) - result["failed"]
    _track(user, "ingest_sheet",
           f"Sheet pull ({result['status']}) — {ok}/{len(result['tabs'])} tabs ingested")
    return {"spreadsheet_id": mr_config.SHEETS_SPREADSHEET_ID, "year": year, **result}


def _persist_sheet_dataset(user_id: str, label: str, metrics, gaps) -> tuple[dict, bool]:
    """Write one tab's dataset run. Returns ``(row, durable)`` — see
    ``runs.save_run``: ``durable`` is False when only the ephemeral disk copy
    was written, which is what gates the swap's delete pass."""
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
    durable = runs.save_run(run)
    return ({"tab": label, "dataset_id": run["id"], "metrics": len(metrics),
             "gaps": run["gaps"]}, durable)


def _ingest_sheet_all(user_id: str, year: int) -> dict:
    """Auto-discovery pull of every tracker tab for one user (UI and cron path).

    Serialised per workspace, then handed to :func:`_pull_and_swap`. Returns
    ``{"tabs", "status", "ingested", "failed", "degraded"}``; raises
    ``SheetPullError`` (nothing touched) or ``SheetPullBusy``.
    """
    lock = _pull_lock(user_id)
    if not lock.acquire(blocking=False):
        raise SheetPullBusy(
            "A sheet pull for this workspace is already running. Two overlapping "
            "pulls interleave their writes and deletes — try again in a moment."
        )
    try:
        return _pull_and_swap(user_id, year)
    finally:
        lock.release()


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
def cron_refresh(request: Request, response: Response):
    """Scheduled full refresh: sheet pull + daily snapshot capture + GCS export.

    Authenticated by the MR_CRON_KEY shared secret (Cloud Scheduler can't hold a
    Firebase user session). The pull runs for MR_CRON_USER_ID's workspace; the
    snapshot capture and exports are user-independent.

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

    uid = os.environ.get("MR_CRON_USER_ID")
    if not uid:
        out["pull"] = "skipped (MR_CRON_USER_ID unset)"
        failures.append("sheet pull skipped: MR_CRON_USER_ID unset")
    else:
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
    _track(run_tracking.CRON_USER, "cron_refresh",
           f"Scheduled refresh for {today.isoformat()}", usage_action="session")
    return _cron_status(response, out, failures, fatal=fatal)


@router.get("/mr/datasets")
def datasets(user=Depends(get_current_user)):
    return [
        {
            "id": r["id"],
            "platform": r.get("platform"),
            "generated_at": r.get("generated_at"),
            "metrics": len(r.get("metrics", [])),
            "leads": len(r.get("leads", [])),
            "gaps": r.get("gaps", []),
        }
        for r in runs.list_runs(user["id"], kind="dataset")
    ]


@router.delete("/mr/datasets/{dataset_id}")
def delete_dataset(dataset_id: str, user=Depends(get_current_user)):
    """Remove one ingested file/pull from the workspace (its numbers leave the
    Overview and future reports immediately)."""
    run = runs.get_run(dataset_id)
    if not run or run.get("user_id") != user["id"] or run.get("kind") != "dataset":
        raise HTTPException(404, "dataset not found")
    runs.delete_run(dataset_id)
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
async def ingest_pdf(file: UploadFile = File(...), user=Depends(get_current_user)):
    """Upload a PDF report: text is extracted locally, metrics are parsed by the
    LLM into the canonical schema and saved as a dataset."""
    import io as _io

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
        "user_id": user["id"],
        "agent_id": MR_AGENT_ID,
        "platform": f"pdf:{name}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "metrics": [m.__dict__ for m in metrics],
        "leads": [],
        "gaps": gaps,
    }
    runs.save_run(run)
    _track(user, "ingest_pdf", f"Parsed PDF “{name}” — {len(metrics)} metrics")
    return {"dataset_id": run["id"], "platform": run["platform"],
            "metrics": len(metrics), "leads": 0, "gaps": gaps}


@router.get("/mr/overview")
def overview(user=Depends(get_current_user)):
    """Live dashboard state — latest-month KPIs vs 2026 goals. Persists nothing."""
    return reports.overview(_load_dataset(user["id"]))


@router.get("/mr/lead-analysis")
def lead_analysis_view(user=Depends(get_current_user)):
    """The lead sheet's per-vendor Meeting Outcome / Deal Stage picture + the
    five lead-quality flags. Data refreshes with every sheet pull (UI or cron)."""
    run = _latest_lead_run(user["id"])
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
    latest = _latest_datasets(user["id"])
    vendor_datasets = [
        {"vendor": plat[7:] if str(plat).startswith("sheets:") else str(plat),
         "metrics": _rehydrate_metrics(run.get("metrics", []))}
        for plat, run in sorted(latest.items())
    ]
    official = _latest_official_run(user["id"])
    return mr_trends.build(vendor_datasets, today=date.today(),
                           official_spend=dict(official.get("months") or {}),
                           official_totals=dict(official.get("totals") or {}))


@router.post("/mr/snapshots/capture")
def snapshots_capture(user=Depends(get_current_user)):
    """Freeze today's MTD state of every tracker tab + refresh the GCS export.
    The daily cron target AND the UI's 'Snapshot now' button."""
    today = date.today()
    try:
        grids = _workbook_grids()
    except Exception as exc:
        raise HTTPException(502, f"Could not read the spreadsheet: {exc}")
    results = mr_snapshots.capture_workbook(grids, year=mr_config.SHEETS_YEAR, today=today)
    exported = mr_snapshots.export_all_to_gcs(today)
    _track(user, "snapshot", f"Captured {len(results)} tab snapshots for {today.isoformat()}")
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
def workbook_scan(user=Depends(get_current_user)):
    """Deep-profile every tab with the LLM and cache the result."""
    try:
        _, profs = _workbook_bundle(deep=True, use_cache=False)
    except Exception as exc:
        raise HTTPException(502, f"Could not read the spreadsheet: {exc}")
    _track(user, "workbook_scan", f"Deep-profiled {len(profs)} workbook tabs")
    return {"tabs": [asdict(p) for p in profs], "count": len(profs)}


@router.post("/mr/ask")
def ask(body: dict | None = None, user=Depends(get_current_user)):
    """Answer a natural-language question with grounded insight from the right tab(s)."""
    body = body or {}
    question = str(body.get("question", "")).strip()
    if not question:
        raise HTTPException(400, "question is required")
    try:
        grids, profs = _workbook_bundle(deep=False)
    except Exception as exc:
        raise HTTPException(502, f"Could not read the spreadsheet: {exc}")
    grid_map = {g.title: g.rows for g in grids}
    answer = mr_insight.answer(
        question, profs, grid_map,
        timeframe=body.get("timeframe"), year=mr_config.SHEETS_YEAR,
    )
    _track(user, "ask", f"Asked: {question}")
    return answer


@router.get("/mr/sources")
def sheet_sources(user=Depends(get_current_user)):
    """Connected workbooks (multi-sheet). The primary tracker is always first."""
    return {
        "enabled": mr_sources_registry.multi_sheet_enabled(),
        "service_account": mr_sources_registry.service_account_email(),
        "sources": mr_sources_registry.list_sources(),
    }


@router.post("/mr/sources")
def add_sheet_source(body: dict | None = None, user=Depends(get_current_user)):
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
        src = mr_sources_registry.add_source(sid, label=str(meta.get("title") or sid))
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    # First-pass understanding, cached per workbook — never fails the add.
    try:
        grids = mr_workbook.fetch_workbook(sid)
        profs = mr_profiles.profile_workbook(grids, year=mr_config.SHEETS_YEAR, deep=False, scope=sid)
        tabs = [asdict(p) for p in profs]
    except Exception:
        tabs = []
    _track(user, "connect_sheet", f"Connected sheet “{src.get('label', sid)}”")
    return {"source": src, "tabs": tabs, "tab_count": len(meta.get("tabs") or [])}


@router.delete("/mr/sources/{spreadsheet_id}")
def remove_sheet_source(spreadsheet_id: str, user=Depends(get_current_user)):
    """Disconnect a secondary sheet — the agent stops reading it immediately."""
    if not mr_sources_registry.multi_sheet_enabled():
        raise HTTPException(403, "Multi-sheet support is disabled on this deployment (MR_MULTI_SHEET).")
    try:
        removed = mr_sources_registry.remove_source(spreadsheet_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not removed:
        raise HTTPException(404, "source not found")
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
    """Effective performance targets/thresholds (defaults merged with edits)."""
    from marketing_research_agent import goals as mr_goals

    return mr_goals.get_targets()


@router.post("/mr/targets")
def save_targets(body: dict | None = None, user=Depends(get_current_user)):
    """Edit targets/figures. Body: {"thresholds": {...}, "channel_goals": {"Google": {...}}}.
    Send {"reset": true} to return to the 2026 defaults."""
    from marketing_research_agent import goals as mr_goals

    body = body or {}
    if body.get("reset"):
        return mr_goals.reset_targets()
    try:
        return mr_goals.set_targets(body)
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@router.get("/mr/config")
def get_config(user=Depends(get_current_user)):
    """Agent configuration: data source, report schedule, and thresholds."""
    from marketing_research_agent import goals as mr_goals

    _thr = mr_goals.thresholds()
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
    panel's month/quarter picker."""
    return reports.available_periods(_load_dataset(user["id"]))


@router.post("/mr/reports/{kind}")
def make_report(kind: str, body: dict | None = None, user=Depends(get_current_user)):
    """Build one report. Body (optional): ``{"period": "2026-07" | "2026-Q3"}`` —
    monthly/quarterly only. An explicit period never substitutes another month's
    data: an empty window is a 422, not a silent fallback."""
    if kind not in reports.KINDS:
        raise HTTPException(404, f"unknown report kind '{kind}' (expected one of {reports.KINDS})")
    period = str((body or {}).get("period") or "").strip() or None
    if period and kind not in ("monthly_summary", "quarterly_summary"):
        raise HTTPException(422, f"'{kind}' reports don't take a period.")
    if kind == "daily_movement":
        report = reports.build(kind, {"snapshot_deltas": mr_snapshots.deltas_for()}, user_id=user["id"])
    else:
        try:
            report = reports.build(kind, _load_dataset(user["id"]), user_id=user["id"], period=period)
        except reports.PeriodError as exc:
            raise HTTPException(422, str(exc))
    _track(user, f"report:{kind}",
           f"Built {kind} report" + (f" for {period}" if period else ""))
    return report


@router.get("/mr/runs")
def list_report_runs(user=Depends(get_current_user)):
    return [
        {"id": r["id"], "kind": r.get("kind"), "generated_at": r.get("generated_at"),
         "period": ((r.get("structured") or {}).get("period") or {}).get("label")}
        for r in runs.list_runs(user["id"], kind=tuple(reports.KINDS))
    ]


@router.get("/mr/runs/{run_id}")
def get_report_run(run_id: str, user=Depends(get_current_user)):
    run = runs.get_run(run_id)
    if not run or run.get("user_id") != user["id"]:
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
def report_run_pdf(run_id: str, user=Depends(get_current_user)):
    """The Reports panel document as a PDF — same sections, same order, same
    figures the user is looking at on screen."""
    run = runs.get_run(run_id)
    if not run or run.get("user_id") != user["id"] or run.get("kind") not in reports.KINDS:
        raise HTTPException(404, "run not found")
    stamp = str(run.get("generated_at", ""))[:10] or date.today().isoformat()
    return _pdf_response(mr_pdf.report_pdf(run), f"mr-{run['kind']}-{stamp}.pdf")


@router.get("/mr/lead-analysis/pdf")
def lead_analysis_pdf(month: str | None = None, user=Depends(get_current_user)):
    """The Leads panel as a PDF — same story line, red-flag card and vendor
    table the user is looking at. ``month`` (YYYY-MM) defaults to the latest;
    an unknown month is a 422, never a silently substituted one."""
    run = _latest_lead_run(user["id"])
    if not run or not (run.get("summary") or {}).get("months"):
        raise HTTPException(404, "no lead-analysis data yet")
    try:
        data = mr_pdf.leads_pdf(run, month=month)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    label = month or (run.get("summary") or {}).get("latest_month") or "latest"
    return _pdf_response(data, f"mr-leads-{label}.pdf")


@router.get("/mr/snapshots/vendor/{slug}/pdf")
def snapshots_vendor_pdf(slug: str, date_iso: str | None = None,
                         user=Depends(get_current_user)):
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
    return _pdf_response(mr_pdf.vendor_pdf(detail, benchmarks),
                         f"mr-vendor-{slug}-{snap_date}.pdf")


@router.post("/mr/schedule/{period}")
def trigger_schedule(period: str, user=Depends(get_current_user)):
    fn = {
        "daily": schedule.run_daily,
        "weekly": schedule.run_weekly,
        "biweekly": schedule.run_biweekly,
        "monthly": schedule.run_monthly,
    }.get(period)
    if not fn:
        raise HTTPException(404, f"unknown period '{period}'")
    result = fn(_load_dataset(user["id"]), user_id=user["id"])
    _track(user, f"schedule:{period}", f"Ran the {period} schedule")
    return result
