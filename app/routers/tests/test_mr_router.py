"""Integration tests for the Marketing Research router (/api/mr).

Runs fully offline: MR_OFFLINE=1 forces the deterministic narrative path and
disables cloud writes; the caller is installed by ``as_caller`` from the shared
harness in ``conftest.py``, which also guarantees the override cannot outlive
the test.

``USER`` stays here because it is more than a login: MR silos every run by
``user_id``, so ``USER["id"]`` is the tenancy key these tests assert reads are
scoped to.
"""

import io
import os

os.environ["MR_OFFLINE"] = "1"

import pytest

from app.routers.tests.conftest import DEFAULT_CALLER, client

USER = dict(DEFAULT_CALLER)

CSV = (
    b"Campaign,Cost,Source,Medium,Campaign name,Leads,Qualified leads,"
    b"Demos booked,Demos completed,Day\n"
    b"PI,1200,google,cpc,pi,12,9,4,2,2026-06-29\n"
)


@pytest.fixture(autouse=True)
def _harness(tmp_path, monkeypatch, as_caller):
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("MR_TARGETS_FILE", str(tmp_path / "targets.json"))
    as_caller(USER)


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


def test_a_failed_cloud_write_keeps_the_superseded_runs(monkeypatch, tmp_path):
    """The swap's success condition is DURABILITY, not "save_run returned".

    Cloud Run's disk is ephemeral, so a replacement whose Firestore write failed
    (oversized doc, quota, contention) lives only on this instance's /tmp. The
    ordering fix put the writes before the deletes; this pins the other half —
    a delete that goes ahead anyway still destroys the only durable copy."""
    from app.routers import marketing_research as mrr
    from marketing_research_agent import runs as mr_runs

    ids = _seed_previous_pull()  # written while offline: disk is the durable store
    monkeypatch.setenv("MR_SOURCES_FILE", str(tmp_path / "sources.json"))
    monkeypatch.setattr(mrr, "fetch_all_trackers", lambda sid, year: _one_tracker_tab())
    monkeypatch.setattr(mrr, "fetch_official_totals",
                        lambda sid, year, **kw: {"2026-06": {"spend": 4200.0}})
    monkeypatch.setattr(mrr.mr_workbook, "fetch_workbook", lambda sid, **kw: [])

    # Cloud-configured and READABLE (the cloud simply holds nothing yet), but
    # every per-document write fails. Reads and writes are faulted separately on
    # purpose: a failed read now aborts the pull outright (see
    # test_an_unreadable_run_store_aborts_the_pull), which would mask the
    # durability guard this test exists to pin.
    class _WriteOnlyDeadDoc:
        def set(self, _payload):
            raise RuntimeError("400 the document exceeds the maximum allowed size")

        def delete(self):
            raise RuntimeError("400 the document exceeds the maximum allowed size")

    class _ReadableCollection:
        def document(self, _id):
            return _WriteOnlyDeadDoc()

        def where(self, **_kw):
            return self

        def stream(self):
            return iter(())

    monkeypatch.setattr(mr_runs, "_use_cloud", lambda: True)
    monkeypatch.setattr(mr_runs, "_collection", _ReadableCollection)

    r = client.post("/api/mr/ingest-sheet", json={})
    assert r.status_code == 207, r.text
    degraded = r.json()["degraded"]
    assert any("could not be stored durably" in d for d in degraded), degraded
    assert mr_runs.get_run(ids["dataset"]) is not None, "durable dataset traded for a /tmp copy"
    assert mr_runs.get_run(ids["official"]) is not None, "durable roll-up traded for a /tmp copy"


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


# --- an unreadable data store answers 502, never an empty dashboard ----------

def _dead_runs_store(monkeypatch):
    """Cloud-configured, but every mr_runs read fails."""
    from marketing_research_agent import runs as mr_runs

    def _dead(*_a, **_kw):
        raise RuntimeError("503 the datastore is unavailable")

    monkeypatch.setattr(mr_runs, "_use_cloud", lambda: True)
    monkeypatch.setattr(mr_runs, "_collection", _dead)


@pytest.mark.parametrize("path", ["/api/mr/overview", "/api/mr/datasets",
                                  "/api/mr/runs", "/api/mr/trends",
                                  "/api/mr/report-periods", "/api/mr/lead-analysis"])
def test_unreadable_run_store_answers_502_not_an_empty_page(monkeypatch, path):
    """Every MR read used to swallow a Firestore failure and render "no data
    yet". The owner cannot tell an outage from an empty workspace that way."""
    _dead_runs_store(monkeypatch)
    r = client.get(path)
    assert r.status_code == 502, f"{path} -> {r.status_code} {r.text}"
    assert "Could not read" in r.json()["detail"]


def test_an_unreadable_run_store_aborts_the_pull_without_deleting_anything(
        monkeypatch, tmp_path):
    """The superseded set is computed from a read. If that read fails and we
    proceed, the replacements land and nothing is ever retired — the workspace
    double-counts for ever. Abort instead, and touch nothing."""
    from app.routers import marketing_research as mrr
    from marketing_research_agent import runs as mr_runs

    ids = _seed_previous_pull()
    monkeypatch.setenv("MR_SOURCES_FILE", str(tmp_path / "sources.json"))
    monkeypatch.setattr(mrr, "fetch_all_trackers", lambda sid, year: _one_tracker_tab())
    monkeypatch.setattr(mrr, "fetch_official_totals", lambda sid, year, **kw: {})
    monkeypatch.setattr(mrr.mr_workbook, "fetch_workbook", lambda sid, **kw: [])
    _dead_runs_store(monkeypatch)

    r = client.post("/api/mr/ingest-sheet", json={})
    assert r.status_code == 502, r.text
    monkeypatch.setattr(mr_runs, "_use_cloud", lambda: False)  # read back offline
    for key in ("dataset", "official", "lead"):
        assert mr_runs.get_run(ids[key]) is not None, f"{key} run was destroyed"


def test_unreadable_snapshot_store_answers_502(monkeypatch):
    from marketing_research_agent import snapshots as mr_snapshots

    monkeypatch.setattr(mr_snapshots, "_use_cloud", lambda: True)
    monkeypatch.setattr(mr_snapshots, "_cloud_list", lambda *a, **kw: None)
    for path in ("/api/mr/snapshots", "/api/mr/snapshots/portfolio",
                 "/api/mr/snapshots/deltas", "/api/mr/snapshots/vendor/meta-360-ra"):
        r = client.get(path)
        assert r.status_code == 502, f"{path} -> {r.status_code} {r.text}"


# --- read cost: one scoped query per request, not three full scans ----------

def _count_run_reads(monkeypatch):
    """Count trips to the run store, and record how each one was scoped."""
    from marketing_research_agent import runs as mr_runs

    calls: list[tuple] = []
    real = mr_runs.list_runs

    def _counted(user_id=None, kind=None):
        calls.append((user_id, kind))
        return real(user_id, kind)

    monkeypatch.setattr(mr_runs, "list_runs", _counted)
    return calls


def test_overview_reads_the_run_store_once(monkeypatch):
    """``_load_dataset`` called ``list_runs`` three times — and each call was an
    unfiltered scan of every workspace's runs. One scoped read now serves all
    three components."""
    _seed_previous_pull()
    calls = _count_run_reads(monkeypatch)
    r = client.get("/api/mr/overview")
    assert r.status_code == 200, r.text
    assert len(calls) == 1, f"{len(calls)} run-store reads for one page: {calls}"
    user_id, kind = calls[0]
    assert user_id == USER["id"], "the read was not scoped to the caller"
    assert kind is not None, "the read did not name the kinds it needs"


@pytest.mark.parametrize("path,kind", [
    ("/api/mr/datasets", "dataset"),
    ("/api/mr/lead-analysis", "lead_analysis"),
])
def test_single_kind_endpoints_ask_for_only_that_kind(monkeypatch, path, kind):
    _seed_previous_pull()
    calls = _count_run_reads(monkeypatch)
    assert client.get(path).status_code == 200
    assert calls == [(USER["id"], kind)], calls


def test_the_runs_list_asks_for_report_kinds_only(monkeypatch):
    _seed_previous_pull()
    calls = _count_run_reads(monkeypatch)
    assert client.get("/api/mr/runs").status_code == 200
    assert len(calls) == 1 and calls[0][0] == USER["id"]
    assert isinstance(calls[0][1], tuple) and "daily_summary" in calls[0][1], calls


def test_the_dataset_read_still_returns_every_component(monkeypatch):
    """Cheap is worthless if it is wrong: the one scoped read must still carry
    the datasets, the official totals AND the lead summary."""
    from app.routers import marketing_research as mrr

    _seed_previous_pull()
    ds = mrr._load_dataset(USER["id"])
    assert ds["official_spend"] == {"2026-07": 8632.0}
    assert ds["lead_summary"] is not None
    assert ds["sources"], "the dataset runs went missing"


# --------------------------------------------------------------------------- #
# Board report (POST /api/mr/board-report)
# --------------------------------------------------------------------------- #
# Two things are being pinned here and they pull in opposite directions: the
# route has to WORK when a deployment enables it, and it has to be completely
# invisible when one has not. Every test below therefore says explicitly which
# state it is in — a board test that forgets to set MR_BOARD_REPORT passes for
# the wrong reason, because 404 is also the answer to "no data yet" on most of
# this router.

#: One month of official roll-up figures, in the shape ``_load_dataset`` reads
#: them out of an ``official_spend`` run. Written straight into the store rather
#: than pulled: a pull needs a live Google workbook, and what this route cares
#: about is only the parsed totals and which ``user_id`` carries them.
BOARD_MONTHS = {
    "2026-01": {"spend": 80000.0, "budget": 82000.0, "leads": 430,
                "qualified_leads": 210, "revenue_clients": 16,
                "revenue_amount_sold": 88000.0, "demos_completed": 90,
                "demos_completed_direct": 82, "qual_demos_booked": 143},
    "2026-02": {"spend": 79000.0, "budget": 81000.0, "leads": 425,
                "qualified_leads": 208, "revenue_clients": 15,
                "revenue_amount_sold": 86000.0, "demos_completed": 89,
                "demos_completed_direct": 81, "qual_demos_booked": 141},
    "2026-03": {"spend": 80581.57, "budget": 85446.0, "leads": 426,
                "qualified_leads": 218, "revenue_clients": 17,
                "revenue_amount_sold": 88947.7, "demos_completed": 93,
                "demos_completed_direct": 85, "qual_demos_booked": 147},
}


def _seed_official(user_id=None, *, captured_at="2026-04-01T00:00:00+00:00",
                   months=None) -> str:
    from marketing_research_agent import runs as mr_runs

    run_id = mr_runs.new_run_id()
    totals = BOARD_MONTHS if months is None else months
    mr_runs.save_run({
        "id": run_id, "kind": "official_spend", "user_id": user_id or USER["id"],
        "agent_id": "a6", "platform": "sheets-official",
        "generated_at": captured_at,
        "months": {k: v["spend"] for k, v in totals.items() if "spend" in v},
        "totals": totals,
    })
    return run_id


@pytest.fixture()
def board_on(monkeypatch):
    """The deployment that has turned the feature on."""
    monkeypatch.setenv("MR_BOARD_REPORT", "1")


def test_the_board_report_route_is_dark_until_a_deployment_enables_it(monkeypatch):
    """Unset switch -> 404, and the SAME 404 an unknown path gives.

    Asserted against a seeded, entirely buildable workspace, so the 404 can only
    be the switch. A "no data" 404 would pass a weaker version of this test
    while the feature was in fact live.
    """
    monkeypatch.delenv("MR_BOARD_REPORT", raising=False)
    _seed_official()
    dark = client.post("/api/mr/board-report", json={"period": "2026-Q1"})
    assert dark.status_code == 404
    # Byte-for-byte what an unrouted path answers. A friendlier detail here
    # ("board reports are disabled") would confirm the feature exists to anyone
    # probing for it, which is the one thing shipping dark is meant to avoid.
    assert dark.json()["detail"] == "Not Found"

    for off in ("0", "false", "off"):
        monkeypatch.setenv("MR_BOARD_REPORT", off)
        assert client.post("/api/mr/board-report",
                           json={"period": "2026-Q1"}).status_code == 404, off


def test_the_board_report_returns_the_ledger_as_data(board_on):
    _seed_official()
    r = client.post("/api/mr/board-report", json={"period": "2026-Q1"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "board_report"
    assert body["user_id"] == USER["id"]

    s = body["structured"]
    assert s["columns"] == ["Q1"]
    assert len(s["rows"]) == 38
    values = {row["key"]: row["value"] for row in s["rows"]}
    assert values["spend"] == 239581.57
    assert values["revenue_clients"] == 48
    # Recomputed from the summed components, never averaged across the months.
    assert values["roas_pct"] == 109.75
    assert values["cac_per_revenue_client"] == 4991.28
    # No HTML and no PDF in this step - the renderer is a separate module.
    assert "html" not in body and "markdown" not in body


def test_the_board_report_names_what_it_could_not_fill_and_why(board_on):
    """The coverage block, over HTTP. Production's stored capture carries 8 of
    43 fields, so most of the catalog is absent; the response has to say which
    rows and why, or a thin capture reads as a quarter where nothing sold."""
    _seed_official()
    body = client.post("/api/mr/board-report", json={"period": "2026-Q1"}).json()
    col = body["structured"]["coverage"]["columns"][0]

    assert col["filled_count"] + len(col["absent"]) == col["metric_count"] == 38
    assert "projected_amount_sold" in col["absent"]
    assert col["absent_reasons"]["projected_amount_sold"]
    values = {row["key"]: row["value"] for row in body["structured"]["rows"]}
    assert values["projected_amount_sold"] is None, "an absent row came back as a number"
    # The channel table has no feed at all, so it is absent rather than zeroed.
    assert body["structured"]["channels"] == []
    assert body["structured"]["coverage"]["channel_reconciliation"].startswith("absent")


def test_the_board_comparison_takes_two_periods(board_on):
    _seed_official(months={**BOARD_MONTHS,
                           "2026-04": dict(BOARD_MONTHS["2026-01"])})
    r = client.post("/api/mr/board-report",
                    json={"period": "2026-01", "compare_to": "2026-04"})
    assert r.status_code == 200, r.text
    s = r.json()["structured"]
    assert r.json()["kind"] == "board_report_comparison"
    assert s["columns"] == ["2026-01", "2026-04"]
    assert s["r_array"][0][0] == "group"


def test_asking_twice_serves_the_stored_report_instead_of_re_deriving(board_on):
    _seed_official()
    first = client.post("/api/mr/board-report", json={"period": "2026-Q1"}).json()
    second = client.post("/api/mr/board-report", json={"period": "2026-Q1"}).json()
    assert first["reused"] is False and second["reused"] is True
    assert second["id"] == first["id"]
    listed = [r for r in client.get("/api/mr/runs").json()
              if r["kind"] == "board_report"]
    assert len(listed) == 1, "a second run was written for an identical request"


def test_a_fresh_sheet_pull_makes_the_next_request_re_derive(board_on):
    """The stale-read failure the key exists to prevent: the sheet is pulled
    again, the figures move, and the report keeps answering with the old ones."""
    _seed_official()
    first = client.post("/api/mr/board-report", json={"period": "2026-Q1"}).json()
    _seed_official(captured_at="2026-04-02T00:00:00+00:00")
    second = client.post("/api/mr/board-report", json={"period": "2026-Q1"}).json()
    assert second["reused"] is False
    assert second["id"] != first["id"]


@pytest.mark.parametrize(("body", "fragment"), [
    ({}, "needs a 'period'"),
    ({"period": "  "}, "needs a 'period'"),
    ({"period": "last quarter"}, "not a board-report period"),
    ({"period": "2026-Q1", "compare_to": "2026-Q1"}, "two different periods"),
])
def test_a_board_request_it_cannot_honour_is_a_422_not_a_guess(board_on, body, fragment):
    _seed_official()
    r = client.post("/api/mr/board-report", json=body)
    assert r.status_code == 422, r.text
    assert fragment in r.json()["detail"], r.json()["detail"]


def test_a_board_report_with_no_capture_says_so_rather_than_publishing_zeros(board_on):
    r = client.post("/api/mr/board-report", json={"period": "2026-Q1"})
    assert r.status_code == 422
    assert "sheet pull" in r.json()["detail"]


def test_the_board_kinds_are_not_buildable_through_the_narrated_report_route():
    """They are real kinds, so ``/mr/reports/{kind}`` recognises them — and has
    to hand them on rather than 500 inside a builder that refuses them."""
    for kind in ("board_report", "board_report_comparison"):
        r = client.post(f"/api/mr/reports/{kind}")
        assert r.status_code == 422, r.text
        assert "/api/mr/board-report" in r.json()["detail"]


def test_there_is_no_pdf_for_a_board_run_yet(board_on):
    """``mr_pdf.report_pdf`` renders a narrative this kind does not have. Until
    the board renderer lands there is no such document, and saying so beats
    streaming a broken one."""
    _seed_official()
    run_id = client.post("/api/mr/board-report", json={"period": "2026-Q1"}).json()["id"]
    assert client.get(f"/api/mr/runs/{run_id}").status_code == 200
    pdf = client.get(f"/api/mr/runs/{run_id}/pdf")
    assert pdf.status_code == 404
    assert "board report" in pdf.json()["detail"]
