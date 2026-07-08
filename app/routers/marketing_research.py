"""Marketing Research agent API (spec §3–§4). Endpoints under ``/api/mr``.

Data enters via CSV/Excel export upload (``/mr/ingest``); the live Google Ads /
META / HubSpot connectors share the same ``DataSource`` interface and slot in
when credentials are provisioned. Reports and ingested datasets are persisted as
runs, owned by the authenticated user.
"""

from __future__ import annotations

import logging
import os
import tempfile
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile

from app.security import get_current_user

from dataclasses import asdict

from marketing_research_agent import config as mr_config
from marketing_research_agent import insight as mr_insight
from marketing_research_agent import profiles as mr_profiles
from marketing_research_agent import reports, runs, schedule
from marketing_research_agent import snapshots as mr_snapshots
from marketing_research_agent import trends as mr_trends
from marketing_research_agent import workbook as mr_workbook
from marketing_research_agent.config import COLUMN_MAPS
from marketing_research_agent.schemas import CampaignMetric, DateRange, Lead
from marketing_research_agent.sources.csv_source import CsvSource
from marketing_research_agent.sources.sheets_source import SheetsSource, fetch_all_trackers

router = APIRouter()
logger = logging.getLogger("agentos.mr")

MR_AGENT_ID = "a6"  # "Market Researcher" slot in the frontend agent catalog
_FULL_RANGE = DateRange(start=date(2000, 1, 1), end=date(2100, 1, 1))


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


def _latest_datasets(user_id: str) -> dict[str, dict]:
    """Newest dataset run per ``platform`` so a re-pull supersedes the prior
    copy rather than double-counting it."""
    latest: dict[str, dict] = {}
    for run in runs.list_runs(user_id):
        if run.get("kind") != "dataset":
            continue
        plat = run.get("platform", run["id"])
        prev = latest.get(plat)
        if prev is None or run.get("generated_at", "") > prev.get("generated_at", ""):
            latest[plat] = run
    return latest


def _load_dataset(user_id: str) -> dict:
    """Reassemble the user's ingested data into one dataset."""
    latest = _latest_datasets(user_id)
    metrics: list[CampaignMetric] = []
    leads: list[Lead] = []
    for run in latest.values():
        metrics.extend(_rehydrate_metrics(run.get("metrics", [])))
        leads.extend(_rehydrate_leads(run.get("leads", [])))
    sources = [
        {"platform": plat, "generated_at": run.get("generated_at"),
         "metrics": len(run.get("metrics", [])), "leads": len(run.get("leads", []))}
        for plat, run in sorted(latest.items())
    ]
    return {"metrics": metrics, "leads": leads, "today": date.today(), "sources": sources}


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
    return {
        "dataset_id": run["id"],
        "platform": platform,
        "metrics": len(metrics),
        "leads": len(leads),
        "gaps": run["gaps"],
    }


@router.post("/mr/ingest-sheet")
def ingest_sheet(
    body: dict | None = None,
    user=Depends(get_current_user),
):
    """Pull the live Google-Sheets performance tracker into datasets.

    Body (all optional): ``{"gid": "...", "brand": "...", "year": 2026}``.
    With a ``gid`` → that single tab is pulled (fast CSV export). With no gid →
    the whole workbook is scanned and every performance-tracker tab is ingested
    (auto-discovery; non-tracker tabs are skipped). Each tab becomes one dataset
    run of channel-aggregate monthly metrics."""
    body = body or {}
    year = int(body.get("year") or mr_config.SHEETS_YEAR)

    # Clear prior sheet datasets for this user so a re-pull is a clean refresh
    # (prevents stale/duplicate datasets — incl. ones from older platform keys).
    for run in runs.list_runs(user["id"]):
        if run.get("kind") == "dataset" and str(run.get("platform", "")).startswith("sheets:"):
            runs.delete_run(run["id"])

    def _persist(label: str, metrics, gaps) -> dict:
        run = {
            "id": runs.new_run_id(),
            "kind": "dataset",
            "user_id": user["id"],
            "agent_id": MR_AGENT_ID,
            "platform": f"sheets:{label}",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "metrics": [m.__dict__ for m in metrics],
            "leads": [],
            "gaps": [g.__dict__ for g in gaps],
        }
        runs.save_run(run)
        return {"tab": label, "dataset_id": run["id"], "metrics": len(metrics), "gaps": run["gaps"]}

    results = []
    if body.get("gid"):
        src = SheetsSource(
            mr_config.SHEETS_SPREADSHEET_ID, str(body["gid"]), year=year, brand=body.get("brand")
        )
        try:
            metrics, gaps = src.fetch_campaign_metrics(_FULL_RANGE)
            results.append(_persist(str(body["gid"]), metrics, gaps))
        except Exception as exc:  # auth/network/format — report, don't 500
            results.append({"tab": str(body["gid"]), "error": str(exc)})
    else:
        try:
            for found in fetch_all_trackers(mr_config.SHEETS_SPREADSHEET_ID, year):
                results.append(_persist(found["tab"], found["metrics"], found["gaps"]))
        except Exception as exc:
            results.append({"tab": "*", "error": str(exc)})

    return {"spreadsheet_id": mr_config.SHEETS_SPREADSHEET_ID, "year": year, "tabs": results}


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
        for r in runs.list_runs(user["id"])
        if r.get("kind") == "dataset"
    ]


@router.get("/mr/overview")
def overview(user=Depends(get_current_user)):
    """Live dashboard state — latest-month KPIs vs 2026 goals. Persists nothing."""
    return reports.overview(_load_dataset(user["id"]))


@router.get("/mr/trends")
def trends_endpoint(user=Depends(get_current_user)):
    """Monthly rollups + deterministic desk insights for the Overview board."""
    latest = _latest_datasets(user["id"])
    vendor_datasets = [
        {"vendor": plat[7:] if str(plat).startswith("sheets:") else str(plat),
         "metrics": _rehydrate_metrics(run.get("metrics", []))}
        for plat, run in sorted(latest.items())
    ]
    return mr_trends.build(vendor_datasets, today=date.today())


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
    return mr_workbook.fetch_workbook(mr_config.SHEETS_SPREADSHEET_ID)


@router.get("/mr/workbook")
def workbook_catalog(user=Depends(get_current_user)):
    """The agent's understanding of every tab (fast heuristic, or cached deep)."""
    try:
        grids = _workbook_grids()
    except Exception as exc:
        raise HTTPException(502, f"Could not read the spreadsheet: {exc}")
    profs = mr_profiles.profile_workbook(grids, year=mr_config.SHEETS_YEAR, deep=False)
    return {"tabs": [asdict(p) for p in profs], "count": len(profs)}


@router.post("/mr/workbook/scan")
def workbook_scan(user=Depends(get_current_user)):
    """Deep-profile every tab with the LLM and cache the result."""
    try:
        grids = _workbook_grids()
    except Exception as exc:
        raise HTTPException(502, f"Could not read the spreadsheet: {exc}")
    profs = mr_profiles.profile_workbook(grids, year=mr_config.SHEETS_YEAR, use_cache=False, deep=True)
    return {"tabs": [asdict(p) for p in profs], "count": len(profs)}


@router.post("/mr/ask")
def ask(body: dict | None = None, user=Depends(get_current_user)):
    """Answer a natural-language question with grounded insight from the right tab(s)."""
    body = body or {}
    question = str(body.get("question", "")).strip()
    if not question:
        raise HTTPException(400, "question is required")
    try:
        grids = _workbook_grids()
    except Exception as exc:
        raise HTTPException(502, f"Could not read the spreadsheet: {exc}")
    profs = mr_profiles.profile_workbook(grids, year=mr_config.SHEETS_YEAR, deep=False)
    grid_map = {g.title: g.rows for g in grids}
    return mr_insight.answer(
        question, profs, grid_map,
        timeframe=body.get("timeframe"), year=mr_config.SHEETS_YEAR,
    )


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


@router.get("/mr/config")
def get_config(user=Depends(get_current_user)):
    """Agent configuration: data source, report schedule, and thresholds."""
    from marketing_research_agent import goals as mr_goals

    return {
        "spreadsheet_id": mr_config.SHEETS_SPREADSHEET_ID,
        "spreadsheet_url": f"https://docs.google.com/spreadsheets/d/{mr_config.SHEETS_SPREADSHEET_ID}/edit",
        "year": mr_config.SHEETS_YEAR,
        "competitors": mr_config.COMPETITORS,
        "schedule": [
            {"report": "Daily Performance Summary", "cadence": "Daily · 3:00 PM PST"},
            {"report": "Weekly Performance Summary", "cadence": "Mondays · 12:00 PM PST"},
            {"report": "Campaign Threshold Alert", "cadence": "Triggered"},
            {"report": "Competitor Change Digest", "cadence": "Weekly"},
            {"report": "Media Opportunity Report", "cadence": "Bi-weekly"},
            {"report": "UTM Attribution Summary", "cadence": "Weekly"},
            {"report": "ICP Audience Signal", "cadence": "Monthly"},
        ],
        "thresholds": {
            "cost_per_booking_flag": mr_goals.COST_PER_BOOKING_FLAG,
            "cac_red": mr_goals.CAC_RED,
            "cost_per_qualified_lead_red": mr_goals.CPQL_RED,
            "spend_no_demo_limit": mr_goals.SPEND_NO_DEMO_LIMIT,
            "conversion_drop_pct": int(mr_goals.CONVERSION_DROP_PCT * 100),
        },
    }


@router.post("/mr/reports/{kind}")
def make_report(kind: str, user=Depends(get_current_user)):
    if kind not in reports.KINDS:
        raise HTTPException(404, f"unknown report kind '{kind}' (expected one of {reports.KINDS})")
    if kind == "daily_movement":
        return reports.build(kind, {"snapshot_deltas": mr_snapshots.deltas_for()}, user_id=user["id"])
    return reports.build(kind, _load_dataset(user["id"]), user_id=user["id"])


@router.get("/mr/runs")
def list_report_runs(user=Depends(get_current_user)):
    return [
        {"id": r["id"], "kind": r.get("kind"), "generated_at": r.get("generated_at")}
        for r in runs.list_runs(user["id"])
        if r.get("kind") in reports.KINDS
    ]


@router.get("/mr/runs/{run_id}")
def get_report_run(run_id: str, user=Depends(get_current_user)):
    run = runs.get_run(run_id)
    if not run or run.get("user_id") != user["id"]:
        raise HTTPException(404, "run not found")
    return run


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
    return fn(_load_dataset(user["id"]), user_id=user["id"])
