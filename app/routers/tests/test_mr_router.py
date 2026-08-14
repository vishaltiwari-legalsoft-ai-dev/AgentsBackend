"""Integration tests for the Marketing Research router (/api/mr).

Runs fully offline: MR_OFFLINE=1 forces the deterministic narrative path and
disables cloud writes; the auth dependency is overridden with a fake user.

The auth override is installed per-test by the ``_harness`` fixture and the
previous value is restored on teardown. It must never be applied at module
import time: ``dependency_overrides`` lives on the one process-global FastAPI
app shared by every test module, so an import-time write leaks into whatever
runs next and a sibling module's teardown silently deletes it — which made this
suite pass only in alphabetical order (a 401 storm under ``-k``, ``-m``,
sharding or random ordering). Same shape as test_geo_router / test_browser_agent_router.
"""

import io
import os

os.environ["MR_OFFLINE"] = "1"

import pytest
from fastapi.testclient import TestClient

from app.main import app as fastapi_app
from app.security import get_current_user

client = TestClient(fastapi_app)

USER = {"id": "u1", "email": "t@legalsoft.com"}

CSV = (
    b"Campaign,Cost,Source,Medium,Campaign name,Leads,Qualified leads,"
    b"Demos booked,Demos completed,Day\n"
    b"PI,1200,google,cpc,pi,12,9,4,2,2026-06-29\n"
)


@pytest.fixture(autouse=True)
def _harness(tmp_path, monkeypatch):
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("MR_TARGETS_FILE", str(tmp_path / "targets.json"))
    prev = fastapi_app.dependency_overrides.get(get_current_user)
    fastapi_app.dependency_overrides[get_current_user] = lambda: dict(USER)
    yield
    if prev is None:
        fastapi_app.dependency_overrides.pop(get_current_user, None)
    else:
        fastapi_app.dependency_overrides[get_current_user] = prev


def test_ingest_then_report():
    r = client.post(
        "/api/mr/ingest",
        files={"file": ("g.csv", io.BytesIO(CSV), "text/csv")},
        data={"platform": "google_ads"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["metrics"] == 1

    rep = client.post("/api/mr/reports/daily_summary")
    assert rep.status_code == 200, rep.text
    assert rep.json()["kind"] == "daily_summary"


def test_unknown_report_kind_404():
    assert client.post("/api/mr/reports/nope").status_code == 404


def test_list_runs_ok():
    assert client.get("/api/mr/runs").status_code == 200


def test_monthly_and_quarterly_reports_build():
    for kind in ("monthly_summary", "quarterly_summary"):
        r = client.post(f"/api/mr/reports/{kind}")
        assert r.status_code == 200, r.text
        assert r.json()["structured"]["period"]["end"]


def test_delete_dataset_removes_it():
    r = client.post(
        "/api/mr/ingest",
        files={"file": ("g.csv", io.BytesIO(CSV), "text/csv")},
        data={"platform": "google_ads"},
    )
    ds_id = r.json()["dataset_id"]
    assert any(d["id"] == ds_id for d in client.get("/api/mr/datasets").json())

    assert client.delete(f"/api/mr/datasets/{ds_id}").status_code == 200
    assert not any(d["id"] == ds_id for d in client.get("/api/mr/datasets").json())
    assert client.delete(f"/api/mr/datasets/{ds_id}").status_code == 404


def test_targets_roundtrip():
    t = client.get("/api/mr/targets").json()
    assert t["edited"] is False and "thresholds" in t and "channel_goals" in t

    r = client.post("/api/mr/targets", json={"thresholds": {"cac_red": 2800}})
    assert r.status_code == 200
    assert r.json()["thresholds"]["cac_red"] == 2800 and r.json()["edited"] is True
    # Config mirrors the edited value.
    assert client.get("/api/mr/config").json()["thresholds"]["cac_red"] == 2800

    assert client.post("/api/mr/targets", json={"thresholds": {"bogus": 1}}).status_code == 400
    assert client.post("/api/mr/targets", json={"reset": True}).json()["edited"] is False


def test_ingest_pdf_offline_stores_dataset():
    """Offline the LLM can't parse metrics — the PDF still lands as a dataset
    with a gap note instead of erroring."""
    from pypdf import PdfWriter

    buf = io.BytesIO()
    w = PdfWriter()
    w.add_blank_page(width=200, height=200)
    w.write(buf)
    r = client.post("/api/mr/ingest-pdf",
                    files={"file": ("report.pdf", io.BytesIO(buf.getvalue()), "application/pdf")})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["platform"] == "pdf:report.pdf"
    assert body["metrics"] == 0 and body["gaps"]

    assert client.post(
        "/api/mr/ingest-pdf",
        files={"file": ("notes.txt", io.BytesIO(b"hi"), "text/plain")},
    ).status_code == 400


def _ingest():
    r = client.post(
        "/api/mr/ingest",
        files={"file": ("g.csv", io.BytesIO(CSV), "text/csv")},
        data={"platform": "google_ads"},
    )
    assert r.status_code == 200, r.text


def test_monthly_report_accepts_explicit_period():
    _ingest()
    r = client.post("/api/mr/reports/monthly_summary", json={"period": "2026-06"})
    assert r.status_code == 200, r.text
    p = r.json()["structured"]["period"]
    assert p["start"] == "2026-06-01" and p["end"] == "2026-06-30"


def test_explicit_period_without_data_is_422_not_wrong_month():
    _ingest()
    r = client.post("/api/mr/reports/monthly_summary", json={"period": "2026-01"})
    assert r.status_code == 422
    assert "January 2026" in r.json()["detail"]


def test_period_rejected_for_other_kinds():
    _ingest()
    assert client.post("/api/mr/reports/daily_summary",
                       json={"period": "2026-06"}).status_code == 422
    assert client.post("/api/mr/reports/daily_movement",
                       json={"period": "2026-06"}).status_code == 422


def test_report_periods_endpoint_lists_data_months():
    _ingest()
    r = client.get("/api/mr/report-periods")
    assert r.status_code == 200, r.text
    months = r.json()["months"]
    assert "2026-06" in [m["period"] for m in months]
    assert {"period", "label", "current"} <= set(months[0])
    assert "2026-Q2" in [q["period"] for q in r.json()["quarters"]]


def test_run_list_includes_period_label():
    _ingest()
    rep = client.post("/api/mr/reports/monthly_summary", json={"period": "2026-06"})
    assert rep.status_code == 200, rep.text
    mine = next(x for x in client.get("/api/mr/runs").json()
                if x["id"] == rep.json()["id"])
    assert mine["period"] == "Jun 1–30, 2026"


def test_lead_analysis_endpoint_before_any_pull_is_honest():
    r = client.get("/api/mr/lead-analysis")
    assert r.status_code == 200
    body = r.json()
    assert body["has_data"] is False and "hint" in body


def test_ingest_sheet_captures_lead_analysis(monkeypatch, tmp_path):
    """The sheet pull auto-detects the lead tab in a connected workbook, persists
    the per-vendor summary, and /mr/lead-analysis serves it."""
    from app.routers import marketing_research as mrr
    from marketing_research_agent.workbook import TabGrid

    header = ["Demo Month", "Campaign", "Brand", "Source", "Meeting Outcome",
              "Deal Stage", "$ Amount", "MRR", "No. of Services Sold"]
    rows = [header,
            ["August", "Meta 360 RA", "RA", "Meta", "Completed", "Contract Sent",
             "$2,000.00", "$2,000.00", "1"],
            ["August", "Meta 360 RA", "RA", "Meta", "No Show", "Demo No Show", "", "", ""]]
    tab = TabGrid(title="Lead Analysis", gid=9, hidden=False, rows=rows,
                  n_rows=len(rows), n_cols=len(header))

    monkeypatch.setenv("MR_SOURCES_FILE", str(tmp_path / "sources.json"))
    monkeypatch.setattr(mrr, "fetch_all_trackers", lambda sid, year: [])
    monkeypatch.setattr(mrr, "fetch_official_totals", lambda sid, year, **kw: {})
    monkeypatch.setattr(mrr.mr_workbook, "fetch_workbook", lambda sid, **kw: [tab])
    monkeypatch.setattr(mrr, "fetch_tab_values", lambda sid, title: rows)

    # 207, not 200: this workbook yields zero tracker tabs, which the pull now
    # reports as degraded (and refuses to treat as "delete every dataset").
    r = client.post("/api/mr/ingest-sheet", json={})
    assert r.status_code == 207, r.text
    assert any(str(t.get("tab", "")).startswith("Lead analysis") for t in r.json()["tabs"])

    body = client.get("/api/mr/lead-analysis").json()
    assert body["has_data"] is True and body["tab"] == "Lead Analysis"
    v = body["months"]["2026-08"]["vendors"][0]
    assert v["booked"] == 2 and v["completed"] == 1 and v["no_show"] == 1
    assert v["services_sold"] == 1 and v["amount"] == 2000.0

    # The Leads panel prints: latest month by default, explicit month by query,
    # honest 422 for a month that has no rows.
    pdf = client.get("/api/mr/lead-analysis/pdf")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
    assert "mr-leads-2026-08.pdf" in pdf.headers["content-disposition"]
    assert client.get("/api/mr/lead-analysis/pdf?month=2026-08").status_code == 200
    assert client.get("/api/mr/lead-analysis/pdf?month=2026-01").status_code == 422


def test_lead_analysis_pdf_404_before_any_pull():
    assert client.get("/api/mr/lead-analysis/pdf").status_code == 404


# --------------- sheet pull: fetch-then-swap + honest status ---------------
# The pull used to delete every sheets:* dataset plus the official and lead runs
# BEFORE it fetched anything, then swallow a fetch failure into a 200. mr_runs is
# the only copy of parsed tracker state and there is no restore path, so one 429
# blanked the dashboard permanently and the only evidence was a response body
# nobody reads. These tests pin the replacement contract.

def _seed_previous_pull():
    """A workspace that already holds a good pull: one tracker dataset, the
    official headline figures, and a lead summary."""
    from marketing_research_agent import runs as mr_runs

    ids = {
        "dataset": mr_runs.new_run_id(),
        "official": mr_runs.new_run_id(),
        "lead": mr_runs.new_run_id(),
    }
    stamp = "2026-08-01T00:00:00+00:00"
    mr_runs.save_run({"id": ids["dataset"], "kind": "dataset", "user_id": USER["id"],
                      "agent_id": "a6", "platform": "sheets:Vendor A", "generated_at": stamp,
                      "metrics": [], "leads": [], "gaps": []})
    mr_runs.save_run({"id": ids["official"], "kind": "official_spend", "user_id": USER["id"],
                      "agent_id": "a6", "platform": "sheets-official", "generated_at": stamp,
                      "months": {"2026-07": 8632.0},
                      "totals": {"2026-07": {"spend": 8632.0}}})
    mr_runs.save_run({"id": ids["lead"], "kind": "lead_analysis", "user_id": USER["id"],
                      "agent_id": "a6", "platform": "sheets-leads", "generated_at": stamp,
                      "source_label": "Primary", "tab": "Lead Analysis", "gaps": [],
                      "summary": {"latest_month": "2026-07",
                                  "months": {"2026-07": {"vendors": [], "flag_count": 0}}}})
    return ids


def _live_run_ids():
    from marketing_research_agent import runs as mr_runs

    return {r["id"] for r in mr_runs.list_runs(USER["id"])}


def _one_tracker_tab():
    from datetime import date

    from marketing_research_agent.schemas import CampaignMetric

    metric = CampaignMetric(
        channel="Google", campaign="pi", utm_source="google", utm_medium="cpc",
        utm_campaign="pi", spend=1200.0, leads=12, qualified_leads=9,
        demos_booked=4, demos_completed=2, date=date(2026, 6, 29),
    )
    return [{"tab": "Vendor A", "gid": 1, "metrics": [metric], "gaps": []}]


def test_failed_tracker_fetch_leaves_every_previous_run_intact(monkeypatch, tmp_path):
    """A 429 from Google must cost nothing: no delete happens before the fetch
    succeeds, and the endpoint says so with a 502 instead of a green 200."""
    from app.routers import marketing_research as mrr

    ids = _seed_previous_pull()
    monkeypatch.setenv("MR_SOURCES_FILE", str(tmp_path / "sources.json"))
    later_calls: list[str] = []

    def _boom(sid, year):
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(mrr, "fetch_all_trackers", _boom)
    monkeypatch.setattr(mrr, "fetch_official_totals",
                        lambda sid, year, **kw: later_calls.append("official") or {})
    monkeypatch.setattr(mrr.mr_workbook, "fetch_workbook",
                        lambda sid, **kw: later_calls.append("workbook") or [])

    r = client.post("/api/mr/ingest-sheet", json={})
    assert r.status_code == 502, r.text
    assert "left untouched" in r.json()["detail"] and "429" in r.json()["detail"]
    assert later_calls == []  # aborted before any further Sheets work
    assert set(ids.values()) <= _live_run_ids()
    assert [d["platform"] for d in client.get("/api/mr/datasets").json()] == ["sheets:Vendor A"]
    assert client.get("/api/mr/lead-analysis").json()["has_data"] is True


def test_successful_pull_still_swaps_old_runs_for_new(monkeypatch, tmp_path):
    """The happy path must still be a clean refresh — superseded runs go, and
    they go only after their replacements are written."""
    from app.routers import marketing_research as mrr
    from marketing_research_agent import runs as mr_runs

    ids = _seed_previous_pull()
    monkeypatch.setenv("MR_SOURCES_FILE", str(tmp_path / "sources.json"))
    monkeypatch.setattr(mrr, "fetch_all_trackers", lambda sid, year: _one_tracker_tab())
    monkeypatch.setattr(mrr, "fetch_official_totals",
                        lambda sid, year, **kw: {"2026-06": {"spend": 5000.0}})
    monkeypatch.setattr(mrr.mr_workbook, "fetch_workbook", lambda sid, **kw: [])  # no lead tab

    r = client.post("/api/mr/ingest-sheet", json={})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["status"] == "ok" and body["ingested"] == 1
    assert body["degraded"] == [] and body["failed"] == 0

    assert not (set(ids.values()) & _live_run_ids())  # every superseded run retired
    datasets = client.get("/api/mr/datasets").json()
    assert [d["platform"] for d in datasets] == ["sheets:Vendor A"]
    assert [d["metrics"] for d in datasets] == [1]
    official = [r for r in mr_runs.list_runs(USER["id"]) if r["kind"] == "official_spend"]
    assert len(official) == 1 and official[0]["months"] == {"2026-06": 5000.0}


def test_official_totals_raise_keeps_the_previous_figures(monkeypatch, tmp_path):
    """Contract C-4: fetch_official_totals now RAISES on a transient Sheets
    failure instead of returning {}. A raise keeps the previous headline figures
    and degrades the response; it must never be read as "no roll-up tab"."""
    from app.routers import marketing_research as mrr
    from marketing_research_agent import runs as mr_runs

    ids = _seed_previous_pull()
    monkeypatch.setenv("MR_SOURCES_FILE", str(tmp_path / "sources.json"))
    monkeypatch.setattr(mrr, "fetch_all_trackers", lambda sid, year: _one_tracker_tab())
    monkeypatch.setattr(mrr.mr_workbook, "fetch_workbook", lambda sid, **kw: [])

    def _boom(sid, year):
        raise RuntimeError("Sheets API 503")

    monkeypatch.setattr(mrr, "fetch_official_totals", _boom)

    r = client.post("/api/mr/ingest-sheet", json={})
    assert r.status_code == 207, r.text
    body = r.json()
    assert body["status"] == "partial"
    assert any("official totals" in d for d in body["degraded"])
    kept = mr_runs.get_run(ids["official"])
    assert kept and kept["months"] == {"2026-07": 8632.0}  # untouched
    # …while the tracker half of the pull still swapped.
    assert ids["dataset"] not in _live_run_ids()


def test_empty_official_totals_retires_the_previous_figures(monkeypatch, tmp_path):
    """The other half of C-4: a clean {} means this workbook genuinely has no
    roll-up tab, so the stale official run IS retired."""
    from app.routers import marketing_research as mrr
    from marketing_research_agent import runs as mr_runs

    ids = _seed_previous_pull()
    monkeypatch.setenv("MR_SOURCES_FILE", str(tmp_path / "sources.json"))
    monkeypatch.setattr(mrr, "fetch_all_trackers", lambda sid, year: _one_tracker_tab())
    monkeypatch.setattr(mrr, "fetch_official_totals", lambda sid, year, **kw: {})
    monkeypatch.setattr(mrr.mr_workbook, "fetch_workbook", lambda sid, **kw: [])

    assert client.post("/api/mr/ingest-sheet", json={}).status_code == 200
    assert mr_runs.get_run(ids["official"]) is None


def test_official_totals_below_the_vendor_tabs_are_rejected(monkeypatch, tmp_path):
    """The 2026-08 failure, as a gate. A layout change re-pointed the Overall
    tab read and it started reporting a fraction of the vendor tabs it sums —
    impossible, since the roll-up aggregates those tabs plus sources of its own.
    The bad figure must never become the headline: previous figures survive and
    the response names both numbers."""
    from app.routers import marketing_research as mrr
    from marketing_research_agent import runs as mr_runs

    ids = _seed_previous_pull()
    monkeypatch.setenv("MR_SOURCES_FILE", str(tmp_path / "sources.json"))
    # _one_tracker_tab() carries $1,200 of Google spend in 2026-06.
    monkeypatch.setattr(mrr, "fetch_all_trackers", lambda sid, year: _one_tracker_tab())
    monkeypatch.setattr(mrr, "fetch_official_totals",
                        lambda sid, year, **kw: {"2026-06": {"spend": 150.0}})
    monkeypatch.setattr(mrr.mr_workbook, "fetch_workbook", lambda sid, **kw: [])

    r = client.post("/api/mr/ingest-sheet", json={})
    assert r.status_code == 207, r.text
    degraded = r.json()["degraded"]
    assert any("do not reconcile" in d for d in degraded), degraded
    assert any("$150.00" in d and "$1,200.00" in d for d in degraded), degraded

    survivor = mr_runs.get_run(ids["official"])
    assert survivor is not None, "a misread roll-up deleted the good figures"
    assert survivor["totals"]["2026-07"]["spend"] == 8632.0


def test_a_reconciling_rollup_is_accepted_as_the_headline(monkeypatch, tmp_path):
    """The healthy shape still swaps: the roll-up is above the vendor tabs."""
    from app.routers import marketing_research as mrr
    from marketing_research_agent import runs as mr_runs

    ids = _seed_previous_pull()
    monkeypatch.setenv("MR_SOURCES_FILE", str(tmp_path / "sources.json"))
    monkeypatch.setattr(mrr, "fetch_all_trackers", lambda sid, year: _one_tracker_tab())
    monkeypatch.setattr(mrr, "fetch_official_totals",
                        lambda sid, year, **kw: {"2026-06": {"spend": 4200.0}})
    monkeypatch.setattr(mrr.mr_workbook, "fetch_workbook", lambda sid, **kw: [])

    r = client.post("/api/mr/ingest-sheet", json={})
    assert r.status_code == 200, r.text
    assert mr_runs.get_run(ids["official"]) is None       # superseded, as designed


def test_a_repeated_month_column_reaches_the_response(monkeypatch, tmp_path):
    """A tab restructured into two month bands is the early warning. The parser
    works around it (leftmost grid wins); the pull must still say so out loud."""
    from datetime import date

    from app.routers import marketing_research as mrr
    from marketing_research_agent.schemas import CampaignMetric, DataGap

    _seed_previous_pull()
    monkeypatch.setenv("MR_SOURCES_FILE", str(tmp_path / "sources.json"))
    metric = CampaignMetric(channel="Google", campaign="p", utm_source="google",
                            utm_medium="cpc", utm_campaign="p", spend=1200.0, leads=1,
                            qualified_leads=0, demos_booked=0, demos_completed=0,
                            date=date(2026, 6, 29))
    gap = DataGap("sheets", "'Vendor A': July appear(s) in more than one column "
                            "band — read the leftmost grid and ignored the repeat(s).")
    monkeypatch.setattr(mrr, "fetch_all_trackers", lambda sid, year: [
        {"tab": "Vendor A", "gid": 1, "metrics": [metric], "gaps": [gap]}])
    monkeypatch.setattr(mrr, "fetch_official_totals", lambda sid, year, **kw: {})
    monkeypatch.setattr(mrr.mr_workbook, "fetch_workbook", lambda sid, **kw: [])

    r = client.post("/api/mr/ingest-sheet", json={})
    assert r.status_code == 207, r.text
    assert any("more than one column band" in d for d in r.json()["degraded"])


def test_empty_tracker_discovery_keeps_previous_datasets(monkeypatch, tmp_path):
    """Zero tracker tabs is suspicious (permissions/format change), not proof the
    vendors were deleted — keep what we have and report it as degraded."""
    from app.routers import marketing_research as mrr

    ids = _seed_previous_pull()
    monkeypatch.setenv("MR_SOURCES_FILE", str(tmp_path / "sources.json"))
    monkeypatch.setattr(mrr, "fetch_all_trackers", lambda sid, year: [])
    monkeypatch.setattr(mrr, "fetch_official_totals", lambda sid, year, **kw: {})
    monkeypatch.setattr(mrr.mr_workbook, "fetch_workbook", lambda sid, **kw: [])

    r = client.post("/api/mr/ingest-sheet", json={})
    assert r.status_code == 207, r.text
    assert any("no tracker tabs" in d for d in r.json()["degraded"])
    assert ids["dataset"] in _live_run_ids()


def test_unreadable_workbook_never_wipes_the_lead_summary(monkeypatch, tmp_path):
    """An unreadable sheet used to look identical to "no lead tab here", which
    retired a good summary on a network blip."""
    from app.routers import marketing_research as mrr
    from marketing_research_agent import runs as mr_runs

    ids = _seed_previous_pull()
    monkeypatch.setenv("MR_SOURCES_FILE", str(tmp_path / "sources.json"))
    monkeypatch.setattr(mrr, "fetch_all_trackers", lambda sid, year: _one_tracker_tab())
    monkeypatch.setattr(mrr, "fetch_official_totals", lambda sid, year, **kw: {})

    def _boom(sid, **kw):
        raise RuntimeError("permission denied")

    monkeypatch.setattr(mrr.mr_workbook, "fetch_workbook", _boom)

    r = client.post("/api/mr/ingest-sheet", json={})
    assert r.status_code == 207, r.text
    assert any("lead analysis" in d for d in r.json()["degraded"])
    assert mr_runs.get_run(ids["lead"]) is not None
    assert client.get("/api/mr/lead-analysis").json()["has_data"] is True


def test_overlapping_pull_is_turned_away_not_interleaved(monkeypatch, tmp_path):
    """Two pulls interleaving their write and delete passes is a data-loss race;
    the cron firing while a user hits Pull is the real-world case."""
    from app.routers import marketing_research as mrr

    monkeypatch.setenv("MR_SOURCES_FILE", str(tmp_path / "sources.json"))
    lock = mrr._pull_lock(USER["id"])
    assert lock.acquire(blocking=False)
    try:
        r = client.post("/api/mr/ingest-sheet", json={})
        assert r.status_code == 409, r.text
        assert "already running" in r.json()["detail"]
    finally:
        lock.release()


def test_single_tab_pull_failure_is_502_not_a_200_with_an_error_inside(monkeypatch):
    from app.routers import marketing_research as mrr

    class _Broken:
        def __init__(self, *a, **kw):
            pass

        def fetch_campaign_metrics(self, _range):
            raise RuntimeError("revoked share")

    monkeypatch.setattr(mrr, "SheetsSource", _Broken)
    r = client.post("/api/mr/ingest-sheet", json={"gid": "42"})
    assert r.status_code == 502, r.text
    assert "revoked share" in r.json()["detail"]


# ----------------------- cron status honesty (C9) -----------------------

def _cron_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MR_CRON_KEY", "s3cret")
    monkeypatch.setenv("MR_CRON_USER_ID", USER["id"])
    monkeypatch.setenv("MR_SOURCES_FILE", str(tmp_path / "sources.json"))


def test_cron_refresh_total_failure_is_not_200(monkeypatch, tmp_path):
    """Cloud Scheduler only reads the status code — a 200 with every stage
    failed inside the body is a job that stays dead for weeks."""
    from app.routers import marketing_research as mrr

    _cron_env(monkeypatch, tmp_path)

    def _boom(*a, **kw):
        raise RuntimeError("revoked service-account share")

    monkeypatch.setattr(mrr, "fetch_all_trackers", _boom)
    monkeypatch.setattr(mrr, "_workbook_grids", _boom)

    assert client.post("/api/mr/cron/refresh").status_code == 403  # auth still closed
    r = client.post("/api/mr/cron/refresh", headers={"x-cron-key": "s3cret"})
    assert r.status_code == 502, r.text
    body = r.json()
    assert body["status"] == "failed" and body["pull"]["status"] == "failed"
    assert any("revoked" in e for e in body["errors"])


def test_cron_refresh_clean_run_is_200(monkeypatch, tmp_path):
    from app.routers import marketing_research as mrr

    _cron_env(monkeypatch, tmp_path)
    monkeypatch.setattr(mrr, "fetch_all_trackers", lambda sid, year: _one_tracker_tab())
    monkeypatch.setattr(mrr, "fetch_official_totals", lambda sid, year, **kw: {})
    monkeypatch.setattr(mrr.mr_workbook, "fetch_workbook", lambda sid, **kw: [])
    monkeypatch.setattr(mrr, "_workbook_grids", lambda: [])
    monkeypatch.setattr(mrr.mr_snapshots, "capture_workbook", lambda grids, **kw: [])
    monkeypatch.setattr(mrr.mr_snapshots, "export_all_to_gcs", lambda today: [])

    r = client.post("/api/mr/cron/refresh", headers={"x-cron-key": "s3cret"})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok" and r.json()["errors"] == []


def test_cron_refresh_unset_user_id_is_not_a_clean_200(monkeypatch, tmp_path):
    """A cron that silently pulls nothing because MR_CRON_USER_ID is unset was
    the "dashboard blank, cron green" trap — it is degraded, not fine."""
    from app.routers import marketing_research as mrr

    _cron_env(monkeypatch, tmp_path)
    monkeypatch.delenv("MR_CRON_USER_ID", raising=False)
    monkeypatch.setattr(mrr, "_workbook_grids", lambda: [])
    monkeypatch.setattr(mrr.mr_snapshots, "capture_workbook", lambda grids, **kw: [])
    monkeypatch.setattr(mrr.mr_snapshots, "export_all_to_gcs", lambda today: [])

    r = client.post("/api/mr/cron/refresh", headers={"x-cron-key": "s3cret"})
    assert r.status_code == 207, r.text
    assert r.json()["status"] == "partial"
