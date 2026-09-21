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

import httpx

os.environ["MR_OFFLINE"] = "1"

import pytest

from app.routers import marketing_research as mr_router
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
    # The workspace key is SERVER configuration. Everything above the "shared MR
    # workspace" section pins the UNSHARED mode — each caller's workbook data is
    # their own — and a machine that happens to export one of these would flip
    # the whole module into shared mode and fail for a reason nobody could see.
    # The shared-mode tests opt in through ``_shared_ws``.
    for var in ("MR_WORKSPACE_ID", "MR_CRON_USER_ID", "MR_WORKSPACE_SHARED",
                "MR_PULL_COOLDOWN_SECONDS"):
        monkeypatch.delenv(var, raising=False)
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

def _seed_previous_pull(user_id=None, stamp="2026-08-01T00:00:00+00:00"):
    """A workspace that already holds a good pull: one tracker dataset, the
    official headline figures, and a lead summary. ``user_id`` is the key the
    runs are stamped with (the caller's own, unless a test is seeding the shared
    workspace's key); ``stamp`` is when they were pulled."""
    from marketing_research_agent import runs as mr_runs

    user_id = user_id or USER["id"]
    ids = {
        "dataset": mr_runs.new_run_id(),
        "official": mr_runs.new_run_id(),
        "lead": mr_runs.new_run_id(),
    }
    mr_runs.save_run({"id": ids["dataset"], "kind": "dataset", "user_id": user_id,
                      "agent_id": "a6", "platform": "sheets:Vendor A", "generated_at": stamp,
                      "metrics": [], "leads": [], "gaps": []})
    mr_runs.save_run({"id": ids["official"], "kind": "official_spend", "user_id": user_id,
                      "agent_id": "a6", "platform": "sheets-official", "generated_at": stamp,
                      "months": {"2026-07": 8632.0},
                      "totals": {"2026-07": {"spend": 8632.0}}})
    mr_runs.save_run({"id": ids["lead"], "kind": "lead_analysis", "user_id": user_id,
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


# --- a period nobody has figures for is refused, and refused before it costs --
# The route used to build ANY period. Five data-free periods returned five 200s
# and minted five runs of 0/38, because a period missing every month records no
# gap and so read as a legitimately empty quarter. The key space is ~170,000
# single periods, every one a guaranteed cache miss, and the idempotency lookup
# linearly scans the workspace's runs - so the junk was self-amplifying and the
# tax fell on every other caller on the instance.


def _board_run_ids() -> list[str]:
    """Every board run in the store, read back through the route that lists
    them. The assertion that matters after a refusal is "nothing was written",
    and that is only meaningful against the persisted set."""
    return [r["id"] for r in client.get("/api/mr/runs").json()
            if r["kind"] in ("board_report", "board_report_comparison")]


@pytest.mark.parametrize("period", ["2026-Q3", "2026-12", "9999-12", "2027"])
def test_a_period_with_no_figures_at_all_is_refused_and_stores_nothing(
        board_on, period):
    """422 like every sibling kind, and - the point of the fix - no run.

    ``/mr/reports/monthly_summary`` has answered 422 to exactly this input since
    it shipped ("No tracker data for January 1900."); the board kinds were the
    two that did not.
    """
    _seed_official()
    assert _board_run_ids() == []
    r = client.post("/api/mr/board-report", json={"period": period})
    assert r.status_code == 422, r.text
    assert "No roll-up figures" in r.json()["detail"], r.json()["detail"]
    assert period in r.json()["detail"]
    assert _board_run_ids() == [], "a refused period still minted a run"


def test_a_comparison_with_no_figures_on_either_side_is_refused(board_on):
    _seed_official()
    r = client.post("/api/mr/board-report",
                    json={"period": "2026-Q3", "compare_to": "2026-Q4"})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "2026-Q3 or 2026-Q4" in detail, detail
    assert "either period" in detail, detail
    assert _board_run_ids() == []


def test_a_comparison_against_an_absent_period_still_builds(board_on):
    """One column of figures is still a report. "Q1 against a quarter we have
    nothing for" is a question someone can legitimately ask, and the coverage
    block already says which column is empty and why - so only a request where
    NEITHER side landed anything is refused."""
    _seed_official()
    r = client.post("/api/mr/board-report",
                    json={"period": "2026-Q1", "compare_to": "2026-Q3"})
    assert r.status_code == 200, r.text
    counts = [c["filled_count"]
              for c in r.json()["structured"]["coverage"]["columns"]]
    assert counts[0] > 0 and counts[1] == 0, counts


def test_a_partly_covered_period_still_builds_and_names_the_missing_month(board_on):
    """The boundary the guard must not cross, and the reason it cannot be a
    bare ``filled_count == 0`` check.

    ``_sum_over`` withholds a field the moment ONE month of the period is
    missing it, so a quarter holding two of three months also fills zero of 38
    - identically to a quarter holding nothing. What separates them is that the
    partial period NAMES the month it withheld for. Refusing on the count alone
    would turn the deliberate "withhold the total, name the month" behaviour
    into a 422.
    """
    _seed_official(months={k: BOARD_MONTHS[k] for k in ("2026-01", "2026-02")})
    r = client.post("/api/mr/board-report", json={"period": "2026-Q1"})
    assert r.status_code == 200, r.text
    s = r.json()["structured"]
    values = {row["key"]: row["value"] for row in s["rows"]}
    assert values["spend"] is None, "two thirds of a quarter published as the quarter"
    assert any("2026-03" in g for g in s["gaps"]), s["gaps"]
    assert s["coverage"]["columns"][0]["filled_count"] == 0
    assert _board_run_ids(), "a buildable period was not stored"


# --- what a 422 is allowed to quote back --------------------------------------

def test_an_unparseable_period_is_quoted_back_clipped_not_whole(board_on):
    """The echo is the useful part of the message and it stays - bounded.

    A period is at most 7 characters, so nothing legitimate is shortened; a
    caller-chosen payload of arbitrary length is no longer reflected whole.
    """
    _seed_official()
    r = client.post("/api/mr/board-report", json={"period": "Q" * 120})
    assert r.status_code == 422, r.text
    echoed = r.json()["detail"].split("'")[1]
    assert len(echoed) <= 40, f"{len(echoed)} characters echoed back"
    assert echoed.endswith("...")
    assert "Q" * 41 not in r.json()["detail"]


@pytest.mark.parametrize("period", [{"x": "y"}, ["2026-Q1"], 2026, True])
def test_a_period_that_is_not_a_string_never_reaches_the_body_as_a_structure(
        board_on, period):
    """A period the caller did not send as text is an absent period, not a
    malformed one. Stringifying it put ``{'x': 'y'}`` into the 422 verbatim."""
    _seed_official()
    r = client.post("/api/mr/board-report", json={"period": period})
    assert r.status_code == 422, r.text
    detail = r.json()["detail"]
    assert "needs a 'period'" in detail, detail
    for fragment in ("{", "[", "x", "y", "2026", "True"):
        assert fragment not in detail.replace("YYYY", ""), (fragment, detail)


def test_the_dark_switch_hides_the_report_not_the_route(board_on, monkeypatch,
                                                        unauthenticated):
    """What the kill switch does and does not buy, pinned so the docstring
    cannot drift back to claiming concealment.

    Auth first: an anonymous caller gets 401 with the switch on AND off, so the
    404 never leaks which state a deployment is in. That ordering is the
    load-bearing claim. But the route is still visibly registered - FastAPI
    parses the body before it resolves dependencies, so a malformed body
    answers 422 here where an unrouted path answers 404.
    """
    unauthenticated()
    bad = {"content": b"{not json", "headers": {"content-type": "application/json"}}
    for switch in ("1", "0"):
        monkeypatch.setenv("MR_BOARD_REPORT", switch)
        assert client.post("/api/mr/board-report",
                           json={"period": "2026-Q1"}).status_code == 401, switch
        assert client.post("/api/mr/board-report", **bad).status_code == 422, switch
    assert client.post("/api/mr/board-report-nope", **bad).status_code == 404


def test_the_board_kinds_are_not_buildable_through_the_narrated_report_route():
    """They are real kinds, so ``/mr/reports/{kind}`` recognises them — and has
    to hand them on rather than 500 inside a builder that refuses them."""
    for kind in ("board_report", "board_report_comparison"):
        r = client.post(f"/api/mr/reports/{kind}")
        assert r.status_code == 422, r.text
        assert "/api/mr/board-report" in r.json()["detail"]


def test_the_narrated_pdf_route_hands_a_board_run_on_rather_than_rendering_it(board_on):
    """``mr_pdf.report_pdf`` renders a narrative this kind does not have, and a
    board report is a different document by a different renderer. The refusal
    names the route that does have it — the same courtesy ``make_report`` pays
    for the build — instead of leaving the caller to guess."""
    _seed_official()
    run_id = client.post("/api/mr/board-report", json={"period": "2026-Q1"}).json()["id"]
    assert client.get(f"/api/mr/runs/{run_id}").status_code == 200
    pdf = client.get(f"/api/mr/runs/{run_id}/pdf")
    assert pdf.status_code == 404
    assert "board report" in pdf.json()["detail"]
    assert f"/api/mr/board-report/{run_id}/pdf" in pdf.json()["detail"]


# --------------------------------------------------------------------------- #
# The board report as a DOCUMENT
# (GET /api/mr/board-report/{run_id}/html and .../pdf)
# --------------------------------------------------------------------------- #
# The HTML route is pure and local. The PDF route is a call to another service,
# and everything below it is about that call failing: unconfigured, unreachable,
# slow, unauthorised, or answering 200 with something that is not a PDF. Every
# one of those must be a loud, specific refusal — there is no local fallback and
# there must never be one, because the only thing available to fall back to is
# ``pdf_export.py``, which renders a different report in a different visual
# identity, and a report that is silently the wrong report is worse than an
# error the user can read.


@pytest.fixture(autouse=True)
def _no_live_renderer(monkeypatch):
    """No test in this module holds a renderer address.

    Unstubbed, ``_render_pdf_via_service`` therefore refuses at
    ``_renderer_config`` and opens no socket at all — which is what makes
    "this suite cannot reach a real renderer" structural rather than a promise
    each test has to keep. ``renderer_on`` opts a test back in, and every test
    that does also stubs ``httpx.post``.
    """
    monkeypatch.delenv("RENDERER_URL", raising=False)
    monkeypatch.delenv("RENDERER_TOKEN", raising=False)


@pytest.fixture()
def renderer_on(monkeypatch):
    """A configured renderer, at an address that cannot resolve.

    ``.invalid`` is reserved by RFC 2606 and never resolves, so even a stub that
    fails to install cannot reach anything real. The backoff goes to zero
    because the retry policy is what is under test, not the wall clock.
    """
    monkeypatch.setenv("RENDERER_URL", "http://renderer.invalid")
    monkeypatch.setenv("RENDERER_TOKEN", "test-token-not-a-real-secret")
    monkeypatch.setattr(mr_router, "_RENDERER_BACKOFF_SECONDS", 0)


def _board_run_id(period: str = "2026-Q1", compare_to: str | None = None) -> str:
    """One stored board run, built through the real route."""
    _seed_official()
    body: dict = {"period": period}
    if compare_to:
        body["compare_to"] = compare_to
    r = client.post("/api/mr/board-report", json=body)
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _stub_renderer(monkeypatch, *responses):
    """Install ``httpx.post`` returning/raising ``responses`` in order.

    Returns the list the stub records each call into, so a test can assert how
    many attempts were made and exactly what was sent.
    """
    calls: list[dict] = []
    queue = list(responses)

    def post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        return answer

    monkeypatch.setattr(mr_router.httpx, "post", post)
    return calls


def _pdf_bytes(size: int = 400) -> bytes:
    return b"%PDF-1.7\n" + b"0" * size + b"\n%%EOF"


def test_the_board_document_routes_are_dark_until_a_deployment_enables_it(monkeypatch):
    """Same kill switch as the POST, and the same 404 an unrouted path gives.

    Asserted against a run that really exists and really is the caller's, so the
    404 can only be the switch.
    """
    monkeypatch.setenv("MR_BOARD_REPORT", "1")
    run_id = _board_run_id()
    monkeypatch.delenv("MR_BOARD_REPORT")

    for suffix in ("html", "pdf"):
        resp = client.get(f"/api/mr/board-report/{run_id}/{suffix}")
        assert resp.status_code == 404, suffix
        assert resp.json()["detail"] == "Not Found", suffix


def test_the_board_document_is_one_self_contained_html_file(board_on):
    """The contract the renderer module exists to hold, asserted at the route.

    No script, no stylesheet link, no remote image, no ``http`` of any kind —
    these files are emailed to clients and the templates this replaces collapsed
    to unstyled text behind a corporate proxy.
    """
    resp = client.get(f"/api/mr/board-report/{_board_run_id()}/html")

    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("text/html")
    assert resp.headers["x-content-type-options"] == "nosniff"
    assert resp.headers["content-disposition"].startswith("inline;")

    body = resp.text
    assert body.startswith("<!DOCTYPE html>")
    for forbidden in ("<script", "<link", "http://", "https://", "http"):
        assert forbidden not in body, forbidden


def test_the_board_document_names_the_brand_rather_than_shipping_the_placeholder(board_on):
    """``render()`` defaults to "Brand not set" so a caller that forgets is
    visible on the page. This is the call site, so it is the one that must not
    forget."""
    from marketing_research_agent import board_report_render as br_render

    body = client.get(f"/api/mr/board-report/{_board_run_id()}/html").text
    assert br_render.UNNAMED_BRAND not in body
    assert "Legal Soft" in body


def test_the_brand_moves_without_a_redeploy(board_on, monkeypatch):
    monkeypatch.setenv("MR_BOARD_REPORT_BRAND", "Acme Legal")
    body = client.get(f"/api/mr/board-report/{_board_run_id()}/html").text
    assert "Acme Legal" in body


def test_the_one_period_and_two_period_documents_are_different_compositions(board_on):
    """The two stored shapes are not the same shape — ``_single_column`` flattens
    a ``PeriodRollup`` and the comparison stores ``ReportLedger.as_dict()`` — so
    both round trips are pinned, and pinned as producing DIFFERENT pages. A
    reconstruction that quietly rendered every run as one period would otherwise
    pass a "it returned 200" test.
    """
    one = client.get(f"/api/mr/board-report/{_board_run_id('2026-01')}/html")
    two = client.get(
        f"/api/mr/board-report/{_board_run_id('2026-Q1', compare_to='2026-02')}/html")

    assert one.status_code == 200 and two.status_code == 200, (one.text, two.text)
    assert "vs" not in one.text.split("<footer>")[-1]
    assert "comparison" in two.text and "vs" in two.text
    assert one.text != two.text


def test_a_board_run_lists_with_its_period_and_not_an_em_dash(board_on):
    """The Reports panel's "Already written" table reads ``period`` per row, and
    a blank there renders as an em-dash — which everywhere else in this UI means
    "not reported". A board run's period is known; it is simply stored under a
    different key, and the list must say so."""
    run_id = _board_run_id("2026-Q1")
    listed = {r["id"]: r for r in client.get("/api/mr/runs").json()}

    assert run_id in listed
    assert listed[run_id]["period"] == "Q1"


def test_a_board_comparison_lists_both_of_its_windows(board_on):
    """One label cannot describe a two-column report, so the row names both — in
    the order the document itself prints them."""
    run_id = _board_run_id("2026-Q1", compare_to="2026-02")
    listed = {r["id"]: r for r in client.get("/api/mr/runs").json()}

    assert listed[run_id]["period"] == "Q1 vs 2026-02"


def test_the_campaign_kinds_period_column_is_untouched(board_on):
    """The board fix reads a second shape; it must not reinterpret the first.

    A monthly summary carries ``period.label`` and has since long before any of
    this, so its row is asserted here beside the board rows rather than trusted
    to stay put.
    """
    client.post(
        "/api/mr/ingest",
        files={"file": ("g.csv", io.BytesIO(CSV), "text/csv")},
        data={"platform": "google_ads"},
    )
    monthly = client.post("/api/mr/reports/monthly_summary")
    assert monthly.status_code == 200, monthly.text
    listed = {r["id"]: r for r in client.get("/api/mr/runs").json()}

    row = listed[monthly.json()["id"]]
    assert row["period"] == monthly.json()["structured"]["period"]["label"]
    assert row["period"]


def test_a_run_that_is_not_a_board_run_has_no_board_document(board_on):
    """The board routes read board kinds. A campaign report's id is not one, and
    the answer is the same 404 an unknown id gets — not a 500 in the renderer."""
    client.post(
        "/api/mr/ingest",
        files={"file": ("g.csv", io.BytesIO(CSV), "text/csv")},
        data={"platform": "google_ads"},
    )
    other = client.post("/api/mr/reports/daily_summary").json()["id"]
    for suffix in ("html", "pdf"):
        resp = client.get(f"/api/mr/board-report/{other}/{suffix}")
        assert resp.status_code == 404, suffix
        assert resp.json()["detail"] == "board report not found"


def test_an_unknown_run_id_is_a_404_not_a_500(board_on):
    assert client.get("/api/mr/board-report/nope/html").status_code == 404
    assert client.get("/api/mr/board-report/nope/pdf").status_code == 404


def test_a_ledger_written_by_another_generator_is_refused_not_half_rendered(board_on):
    """``GENERATOR_VERSION`` is a cache invalidation for the builder and a shape
    warning for the renderer: a run stored by an older generator may not carry
    the fields this reconstruction reads. Refuse it and say to rebuild, rather
    than render whichever half survived."""
    from marketing_research_agent import runs as mr_runs

    run_id = _board_run_id()
    stored = mr_runs.get_run(run_id)
    stored["structured"]["generator"] = "mr-board-report/1"
    mr_runs.save_run(stored)

    resp = client.get(f"/api/mr/board-report/{run_id}/html")
    assert resp.status_code == 422, resp.text
    assert "mr-board-report/1" in resp.json()["detail"]
    assert "POST /api/mr/board-report" in resp.json()["detail"]


def test_a_structurally_broken_ledger_does_not_publish_the_stores_schema(board_on):
    """The stored shape is ours, not the caller's. A missing field is a 422 the
    caller can act on and a stack trace in the log — never the ``KeyError``'s
    field name echoed into the response body."""
    from marketing_research_agent import runs as mr_runs

    run_id = _board_run_id()
    stored = mr_runs.get_run(run_id)
    del stored["structured"]["periods"]
    mr_runs.save_run(stored)

    resp = client.get(f"/api/mr/board-report/{run_id}/html")
    assert resp.status_code == 422, resp.text
    assert "periods" not in resp.json()["detail"]
    assert "rebuild it at POST /api/mr/board-report" in resp.json()["detail"]


# --- the renderer is another service, and it is treated as hostile -----------

def test_this_suite_cannot_reach_a_real_renderer(board_on):
    """Offline, proven from inside pytest rather than assumed.

    The repo-root guard is fixture-ordering-dependent and has leaked twice, so
    the three things this section rests on are asserted here, in a running test:
    the process holds no renderer address, live Firestore is blocked, and an
    unstubbed PDF request therefore refuses at the config check before any
    socket is opened.
    """
    from app.services import firestore_repo

    assert os.environ.get("RENDERER_URL") is None
    assert os.environ.get("RENDERER_TOKEN") is None
    with pytest.raises(RuntimeError, match="blocked in tests"):
        firestore_repo._db()

    resp = client.get(f"/api/mr/board-report/{_board_run_id()}/pdf")
    assert resp.status_code == 503, resp.text


def test_an_unconfigured_renderer_is_a_503_naming_both_variables(board_on):
    resp = client.get(f"/api/mr/board-report/{_board_run_id()}/pdf")

    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert "RENDERER_URL" in detail and "RENDERER_TOKEN" in detail
    assert "are unset" in detail
    # And it points at the document that IS available without the renderer.
    assert "/html" in detail


def test_it_names_the_half_of_the_configuration_that_is_missing(board_on, monkeypatch):
    """Half-configured is the case that actually happens — a URL set in one
    deploy and the secret forgotten. Naming both would send someone to check a
    variable that is fine."""
    monkeypatch.setenv("RENDERER_URL", "http://renderer.invalid")
    detail = client.get(
        f"/api/mr/board-report/{_board_run_id()}/pdf").json()["detail"]

    assert "RENDERER_TOKEN is unset" in detail
    assert "RENDERER_URL" not in detail


def test_the_pdf_is_exactly_what_the_renderer_returned(board_on, renderer_on, monkeypatch):
    """And the request it was sent is the contract the renderer checks: ``v: 1``,
    the document verbatim, and the shared secret in the header and nowhere
    else."""
    pdf = _pdf_bytes()
    calls = _stub_renderer(monkeypatch, httpx.Response(200, content=pdf))

    run_id = _board_run_id()
    html = client.get(f"/api/mr/board-report/{run_id}/html").text
    resp = client.get(f"/api/mr/board-report/{run_id}/pdf")

    assert resp.status_code == 200, resp.text
    assert resp.content == pdf
    assert resp.headers["content-type"] == "application/pdf"
    assert "attachment" in resp.headers["content-disposition"]

    assert len(calls) == 1
    assert calls[0]["url"] == "http://renderer.invalid/pdf"
    assert calls[0]["json"] == {"v": 1, "html": html}
    assert calls[0]["headers"] == {"X-Renderer-Token": "test-token-not-a-real-secret"}
    assert calls[0]["timeout"] is not None, "an external call with no timeout"


def test_an_unreachable_renderer_is_a_loud_502_and_never_a_substitute_document(
        board_on, renderer_on, monkeypatch):
    """The whole point of this route's failure path. No reportlab export, no
    HTML under a ``.pdf`` name, no empty 200 — a 502 that says the renderer
    could not be reached."""
    calls = _stub_renderer(monkeypatch, httpx.ConnectError("no route to host"))

    resp = client.get(f"/api/mr/board-report/{_board_run_id()}/pdf")

    assert resp.status_code == 502, resp.text
    assert not resp.content.startswith(b"%PDF")
    assert "RENDERER_URL" in resp.json()["detail"]
    assert len(calls) == 2, "a connection failure is retried exactly once"


def test_a_cold_renderer_is_retried_once_and_then_serves_the_document(
        board_on, renderer_on, monkeypatch):
    """min-instances=0 plus a Chromium launch means the first request of the day
    can legitimately be refused while the instance comes up."""
    pdf = _pdf_bytes()
    calls = _stub_renderer(
        monkeypatch, httpx.ConnectError("connection refused"),
        httpx.Response(200, content=pdf))

    resp = client.get(f"/api/mr/board-report/{_board_run_id()}/pdf")

    assert resp.status_code == 200, resp.text
    assert resp.content == pdf
    assert len(calls) == 2


def test_a_read_timeout_is_a_504_and_is_not_retried_into_a_double_wait(
        board_on, renderer_on, monkeypatch):
    """A render that ran out of time will run out of time again. Retrying it
    only doubles what the caller waits before hearing the same answer."""
    calls = _stub_renderer(monkeypatch, httpx.ReadTimeout("too slow"))

    resp = client.get(f"/api/mr/board-report/{_board_run_id()}/pdf")

    assert resp.status_code == 504, resp.text
    assert "RENDERER_TIMEOUT_SECONDS" in resp.json()["detail"]
    assert len(calls) == 1


def test_the_renderer_rejecting_our_token_never_echoes_it(
        board_on, renderer_on, monkeypatch):
    _stub_renderer(monkeypatch, httpx.Response(401, json={"error": "unauthorized"}))

    resp = client.get(f"/api/mr/board-report/{_board_run_id()}/pdf")

    assert resp.status_code == 502, resp.text
    assert "RENDERER_TOKEN" in resp.json()["detail"]
    assert "test-token-not-a-real-secret" not in resp.text


def test_the_renderers_own_fail_closed_503_stays_a_503(
        board_on, renderer_on, monkeypatch):
    """The renderer answers 503 when its OWN ``RENDERER_TOKEN`` is unset. That is
    a configuration answer, not a transport one, so it is passed through as 503
    and — being a deliberate refusal — is not retried."""
    calls = _stub_renderer(
        monkeypatch, httpx.Response(503, json={"error": "pdf rendering unconfigured"}))

    resp = client.get(f"/api/mr/board-report/{_board_run_id()}/pdf")

    assert resp.status_code == 503, resp.text
    assert "RENDERER_TOKEN" in resp.json()["detail"]
    assert len(calls) == 1


def test_a_200_carrying_something_that_is_not_a_pdf_is_refused(
        board_on, renderer_on, monkeypatch):
    """The fake-success shape: a proxy's error page streamed back under
    ``application/pdf``. A 200 is not a PDF; ``%PDF`` is."""
    _stub_renderer(monkeypatch, httpx.Response(200, content=b"<html>gateway error</html>"))

    resp = client.get(f"/api/mr/board-report/{_board_run_id()}/pdf")

    assert resp.status_code == 502, resp.text
    assert "not a PDF" in resp.json()["detail"]


def test_a_document_that_lost_its_self_containment_says_so_on_the_response(
        board_on, renderer_on, monkeypatch):
    """The renderer blocks every subresource and reports how many it blocked.
    Non-zero means the document grew an external dependency and is rendering
    wrong — visible on the response and loud in the log, never swallowed."""
    _stub_renderer(monkeypatch, httpx.Response(
        200, content=_pdf_bytes(), headers={"x-blocked-subresources": "3"}))

    resp = client.get(f"/api/mr/board-report/{_board_run_id()}/pdf")

    assert resp.status_code == 200, resp.text
    assert resp.headers["x-blocked-subresources"] == "3"


# --------------------------------------------------------------------------- #
# The run store has a lifecycle — see marketing_research_agent/runs.py
# --------------------------------------------------------------------------- #

def test_the_report_route_no_longer_grows_without_bound(monkeypatch):
    """``POST /mr/reports/{kind}`` mints a fresh uuid run per call — no dedup,
    no cache key, and it has been live far longer than any guard. It is not the
    route that bounds it: retention sits at ``runs.save_run``, the one choke
    point every kind and every route goes through, so this pre-existing route
    is covered without being touched.

    Asserted through ``GET /mr/runs`` — the Reports panel's own list — because
    the number that matters is what a workspace actually accumulates."""
    monkeypatch.setenv("MR_RUN_RETENTION_PER_KIND", "3")
    client.post("/api/mr/ingest",
                files={"file": ("g.csv", io.BytesIO(CSV), "text/csv")},
                data={"platform": "google_ads"})

    built = []
    for _ in range(8):
        r = client.post("/api/mr/reports/daily_summary")
        assert r.status_code == 200, r.text
        built.append(r.json()["id"])

    listed = client.get("/api/mr/runs")
    assert listed.status_code == 200, listed.text
    daily = [r for r in listed.json() if r["kind"] == "daily_summary"]
    assert len(daily) == 3, f"{len(daily)} daily_summary runs kept, expected 3"
    assert {r["id"] for r in daily} == set(built[-3:])
    # The newest is still readable end to end, which is what the panel does next.
    assert client.get(f"/api/mr/runs/{built[-1]}").status_code == 200
    assert client.get(f"/api/mr/runs/{built[0]}").status_code == 404


def test_report_churn_never_costs_the_workspace_its_data(monkeypatch):
    """The hard constraint, at the surface it would break: the ingested dataset
    IS the workspace's numbers (``mr_runs`` is the only copy of parsed tracker
    state), so it is exempt from the cap. A user building report after report
    must still see their data afterwards."""
    monkeypatch.setenv("MR_RUN_RETENTION_PER_KIND", "1")
    up = client.post("/api/mr/ingest",
                     files={"file": ("g.csv", io.BytesIO(CSV), "text/csv")},
                     data={"platform": "google_ads"})
    dataset_id = up.json()["dataset_id"]

    for kind in ("daily_summary", "weekly_summary", "monthly_summary",
                 "daily_summary", "daily_summary"):
        assert client.post(f"/api/mr/reports/{kind}").status_code == 200

    datasets = client.get("/api/mr/datasets")
    assert [d["id"] for d in datasets.json()] == [dataset_id], (
        "the ingested dataset was evicted — the workspace just lost its numbers")
    overview = client.get("/api/mr/overview")
    assert overview.status_code == 200, overview.text
    assert overview.json()["has_data"] is True


def test_several_uploads_all_survive_the_cap(monkeypatch):
    """More datasets than the cap, on purpose: a tracker pull writes one run per
    tab (eleven, live) and every one of them is STORED and listed. A cap that
    applied here would silently delete vendors' runs.

    Stored is not the same as counted. ``_load_dataset`` feeds a report from the
    NEWEST run per platform only (``_latest_datasets``), so these five
    same-platform uploads all survive the cap but only the last one contributes
    to any figure — the other four are ``superseded`` in the listing, which is
    what ``test_the_datasets_list_marks_the_runs_that_no_longer_count`` pins."""
    monkeypatch.setenv("MR_RUN_RETENTION_PER_KIND", "2")
    for n in range(5):
        r = client.post("/api/mr/ingest",
                        files={"file": (f"g{n}.csv", io.BytesIO(CSV), "text/csv")},
                        data={"platform": "google_ads"})
        assert r.status_code == 200, r.text
    listed = client.get("/api/mr/datasets").json()
    assert len(listed) == 5
    assert sum(1 for d in listed if not d["superseded"]) == 1, "only the newest one counts"


# --------------------------------------------------------------------------- #
# The shared MR workspace
# --------------------------------------------------------------------------- #
# Everything above this line runs with NO workspace key configured, so each
# caller's workbook data is their own — that is the unshared mode, and it is what
# local, dev and this module's harness default to.
#
# Everything below turns sharing ON — a workspace key (``MR_CRON_USER_ID``, the
# account the 15-minute cron pulls for) AND the explicit opt-in
# (``MR_WORKSPACE_SHARED=1``) — and pins what changes: the three
# workbook-derived kinds and the board report become ONE copy every signed-in
# member reads, while the reports a person builds, their run list, their targets
# and their schedule stay their own. Why: on production only the cron account's
# copy was ever fresh, so every other account opened an empty Overview, an empty
# month picker and an empty board-report builder in front of a workbook the
# whole team already shares.
#
# The switch is an opt-in that FAILS CLOSED: unset, or any value the code does
# not positively recognise, is OFF. So the key alone — which production already
# has — changes nothing (``test_deploying_without_the_switch_changes_nothing``),
# and a typo in the switch changes nothing
# (``test_a_typo_in_the_switch_fails_closed``). Rollback is unsetting it (or 0).
#
# While shared, what changes the WHOLE team's dashboard is an admin's: uploads,
# a forced pull and a single-tab pull are 403 for a member. Those tests say so.
#
# The key is a THIRD id, neither caller. A test that pulls as USER and reads as
# MEMBER would also pass if MEMBER were simply reading USER's rows; with a
# workspace id that belongs to nobody, a pass can only come from the resolver.

#: The deployment's workspace key in these tests — the cron's account.
WORKSPACE = "mr-shared-workspace"

MEMBER = {"id": "u2", "email": "member@legalsoft.com", "is_admin": False,
          "is_creator": False, "session_id": "", "timezone": "UTC"}

#: An admin. Everything that changes what the whole team sees is theirs.
ADMIN = {"id": "u-admin", "email": "admin@legalsoft.com", "is_admin": True,
         "is_creator": False, "session_id": "", "timezone": "UTC"}


def _shared_ws(monkeypatch, key=WORKSPACE):
    """Turn the shared workspace ON: the cron's account becomes the workspace AND
    the opt-in switch is set. Both are needed — see the section header."""
    monkeypatch.setenv("MR_CRON_USER_ID", key)
    monkeypatch.setenv("MR_WORKSPACE_SHARED", "1")
    return key


def _lead_tab():
    """A workbook tab the lead-analysis auto-detection recognises by its header."""
    from marketing_research_agent.workbook import TabGrid

    header = ["Demo Month", "Campaign", "Brand", "Source", "Meeting Outcome",
              "Deal Stage", "$ Amount", "MRR", "No. of Services Sold"]
    rows = [header,
            ["August", "Meta 360 RA", "RA", "Meta", "Completed", "Contract Sent",
             "$2,000.00", "$2,000.00", "1"],
            ["August", "Meta 360 RA", "RA", "Meta", "No Show", "Demo No Show", "", "", ""]]
    return TabGrid(title="Lead Analysis", gid=9, hidden=False, rows=rows,
                   n_rows=len(rows), n_cols=len(header))


def _stub_pull(monkeypatch, tmp_path, *, lead_tab=None):
    """Stub every Google read a full pull makes; nothing here leaves the process.

    Returns the list each tracker fetch is recorded into, so a test can assert
    that a skipped pull fetched NOTHING — the cost the cooldown exists to avoid.
    """
    fetches: list[str] = []
    monkeypatch.setenv("MR_SOURCES_FILE", str(tmp_path / "sources.json"))

    def _tracker(sid, year):
        fetches.append(sid)
        return _one_tracker_tab()

    monkeypatch.setattr(mr_router, "fetch_all_trackers", _tracker)
    monkeypatch.setattr(mr_router, "fetch_official_totals",
                        lambda sid, year, **kw: {"2026-06": {"spend": 5000.0}})
    monkeypatch.setattr(mr_router.mr_workbook, "fetch_workbook",
                        lambda sid, **kw: [] if lead_tab is None else [lead_tab])
    if lead_tab is not None:
        monkeypatch.setattr(mr_router, "fetch_tab_values", lambda sid, title: lead_tab.rows)
    return fetches


def _stub_cron_extras(monkeypatch):
    """The snapshot/export half of the cron, stubbed clean."""
    monkeypatch.setenv("MR_CRON_KEY", "s3cret")
    monkeypatch.setattr(mr_router, "_workbook_grids", lambda: [])
    monkeypatch.setattr(mr_router.mr_snapshots, "capture_workbook", lambda grids, **kw: [])
    monkeypatch.setattr(mr_router.mr_snapshots, "export_all_to_gcs", lambda today: [])


def _stored_pull_stamps(key):
    """``generated_at`` of every run under ``key`` that a FULL pull produced — the
    ``official_spend`` runs, which nothing but ``_pull_and_swap`` writes and which
    are what the freshness clock reads."""
    from marketing_research_agent import runs as mr_runs

    return [r["generated_at"] for r in mr_runs.list_runs(key, kind="official_spend")]


def _ago(seconds: float) -> str:
    """An ISO stamp ``seconds`` in the past, for seeding a pull of a known age."""
    from datetime import datetime, timedelta, timezone

    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


def _stub_single_tab(monkeypatch, *, tabs=None, values=None):
    """Stub the two Sheets reads the SHARED single-tab pull makes (the tab list
    and the tab's values) and record every call. ``tabs`` is the workbook's tab
    list; ``values`` maps a tab title to its rows (default: a real tracker)."""
    calls: list[tuple] = []
    tab_list = tabs if tabs is not None else [
        {"gid": 1, "title": "Vendor A", "hidden": False},
        {"gid": 2, "title": "Marketing 2026 Overall Report", "hidden": False},
        {"gid": 3, "title": "Old Archive", "hidden": True},
        {"gid": 4, "title": "Vendor B", "hidden": False},
    ]

    def _meta(sid, **kw):
        calls.append(("meta", sid))
        return {"title": "Tracker", "tabs": tab_list}

    def _values(sid, title, **kw):
        calls.append(("values", title))
        return (values or {}).get(title, _tracker_rows(title))

    monkeypatch.setattr(mr_router, "workbook_meta", _meta)
    monkeypatch.setattr(mr_router, "fetch_tab_values", _values)
    return calls


def _tracker_rows(title="Vendor A"):
    """A tab ``parse_tracker`` reads as a vendor tracker: the vendor in A1, a
    month header per column, and a spend + leads row (June $1,200, July $900)."""
    return [
        [title, "Jun (Performance)", "Jul (Performance)"],
        ["Spend", "$1,200.00", "$900.00"],
        ["Leads", "12", "9"],
    ]


def test_a_second_member_reads_the_pull_a_colleague_triggered(monkeypatch, tmp_path, as_caller):
    """The bug, end to end. One member presses Pull; a different member — who has
    never pulled and has no data of their own — opens the same panels and sees
    the numbers. Before this, they saw a blank workspace."""
    from marketing_research_agent import runs as mr_runs

    _shared_ws(monkeypatch)
    _stub_pull(monkeypatch, tmp_path)

    as_caller(USER)
    pulled = client.post("/api/mr/ingest-sheet", json={})
    assert pulled.status_code == 200, pulled.text

    # Where it landed: the workspace key, not whoever pressed the button.
    assert [r["platform"] for r in mr_runs.list_runs(WORKSPACE, kind="dataset")] == [
        "sheets:Vendor A"]
    assert [r["kind"] for r in mr_runs.list_runs(WORKSPACE, kind="official_spend")] == [
        "official_spend"]
    assert mr_runs.list_runs(USER["id"]) == [], "the pull was stamped with the caller"

    as_caller(MEMBER)
    assert client.get("/api/mr/overview").json()["has_data"] is True
    assert client.get("/api/mr/trends").json()["has_data"] is True
    months = [m["period"] for m in client.get("/api/mr/report-periods").json()["months"]]
    assert "2026-06" in months
    assert [d["platform"] for d in client.get("/api/mr/datasets").json()] == ["sheets:Vendor A"]


def test_the_cron_pull_is_what_every_signed_in_member_reads(monkeypatch, tmp_path, as_caller):
    """The production shape: nobody signed in pulls; the scheduler does, for
    ``MR_CRON_USER_ID``. Neither member below is that account, and both read
    the result — including the lead analysis, whose flags were judged at pull
    time."""
    _shared_ws(monkeypatch)
    _stub_pull(monkeypatch, tmp_path, lead_tab=_lead_tab())
    _stub_cron_extras(monkeypatch)

    fired = client.post("/api/mr/cron/refresh", headers={"x-cron-key": "s3cret"})
    assert fired.status_code == 200, fired.text
    assert fired.json()["errors"] == []

    for who in (USER, MEMBER):
        as_caller(who)
        assert client.get("/api/mr/overview").json()["has_data"] is True, who["id"]
        periods = client.get("/api/mr/report-periods").json()
        assert periods["months"] and periods["quarters"], who["id"]
        lead = client.get("/api/mr/lead-analysis").json()
        assert lead["has_data"] is True and lead["tab"] == "Lead Analysis", who["id"]


def test_an_explicit_workspace_id_is_the_key_the_cron_writes_under(
        monkeypatch, tmp_path, as_caller):
    """``MR_WORKSPACE_ID`` outranks the cron account for READS, so the cron must
    write under it too — otherwise a deployment that sets it would refresh a key
    nobody reads, which is the blank dashboard again with a green cron."""
    from marketing_research_agent import runs as mr_runs

    _shared_ws(monkeypatch)
    monkeypatch.setenv("MR_WORKSPACE_ID", "team-workspace")
    _stub_pull(monkeypatch, tmp_path)
    _stub_cron_extras(monkeypatch)

    assert client.post("/api/mr/cron/refresh",
                       headers={"x-cron-key": "s3cret"}).status_code == 200
    assert mr_runs.list_runs("team-workspace", kind="dataset")
    assert mr_runs.list_runs(WORKSPACE) == [], "the cron wrote under the cron account"

    as_caller(MEMBER)
    assert client.get("/api/mr/overview").json()["has_data"] is True


def test_a_pull_inside_the_cooldown_serves_the_fresh_one_and_fetches_nothing(
        monkeypatch, tmp_path, as_caller):
    """Every member can press Pull, and the in-process lock only serialises pulls
    on one Cloud Run instance. Without a freshness gate a team of nineteen is
    nineteen Google fetches. The answer is honest, not a fake success: nothing
    was fetched, the body says so, and it names when the data on screen was
    pulled."""
    from datetime import datetime

    from marketing_research_agent import runs as mr_runs

    _shared_ws(monkeypatch)
    fetches = _stub_pull(monkeypatch, tmp_path)

    assert client.post("/api/mr/ingest-sheet", json={}).status_code == 200
    assert len(fetches) == 1
    before = {r["id"] for r in mr_runs.list_runs(WORKSPACE)}

    as_caller(MEMBER)
    again = client.post("/api/mr/ingest-sheet", json={})
    assert again.status_code == 200, again.text
    body = again.json()
    assert body["status"] == "fresh"
    assert (body["tabs"], body["ingested"], body["failed"], body["degraded"]) == ([], 0, 0, [])
    newest = max(_stored_pull_stamps(WORKSPACE), key=datetime.fromisoformat)
    assert body["last_pulled_at"] == newest, "it must name the pull it is pointing at"
    assert len(fetches) == 1, "the cooldown still went to Google"
    assert {r["id"] for r in mr_runs.list_runs(WORKSPACE)} == before, (
        "a skipped pull wrote or deleted runs")


def test_a_pull_older_than_the_cooldown_is_pulled_not_skipped(monkeypatch, tmp_path):
    """The other half: freshness is a window, not a latch."""
    _shared_ws(monkeypatch)
    fetches = _stub_pull(monkeypatch, tmp_path)
    _seed_previous_pull(WORKSPACE, stamp="2026-08-01T00:00:00+00:00")

    r = client.post("/api/mr/ingest-sheet", json={})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok" and r.json()["ingested"] == 1
    assert len(fetches) == 1


def test_a_run_stamped_in_the_future_never_blocks_a_pull(monkeypatch, tmp_path):
    """A stamp ahead of the clock is skew (or bad data), not freshness. Treated
    as fresh it would refuse every pull until the year it names."""
    _shared_ws(monkeypatch)
    fetches = _stub_pull(monkeypatch, tmp_path)
    _seed_previous_pull(WORKSPACE, stamp="2099-01-01T00:00:00+00:00")

    r = client.post("/api/mr/ingest-sheet", json={})
    assert r.status_code == 200 and r.json()["status"] == "ok", r.text
    assert len(fetches) == 1


def test_an_upload_does_not_make_the_next_pull_fresh(monkeypatch, tmp_path, as_caller):
    """``last_pulled_at`` means WHEN THE SHEET WAS PULLED. A CSV an admin
    uploaded a minute ago is not a pull, and counting it would answer "fresh"
    with an upload's timestamp while the tracker data was stale."""
    _shared_ws(monkeypatch)
    fetches = _stub_pull(monkeypatch, tmp_path)

    as_caller(ADMIN)                       # uploads are an admin's while shared
    up = client.post("/api/mr/ingest", files={"file": ("g.csv", io.BytesIO(CSV), "text/csv")},
                     data={"platform": "google_ads"})
    assert up.status_code == 200, up.text

    as_caller(MEMBER)
    pulled = client.post("/api/mr/ingest-sheet", json={})
    assert pulled.status_code == 200 and pulled.json()["status"] == "ok", pulled.text
    assert len(fetches) == 1


def test_the_cooldown_can_be_switched_off(monkeypatch, tmp_path):
    _shared_ws(monkeypatch)
    monkeypatch.setenv("MR_PULL_COOLDOWN_SECONDS", "0")
    fetches = _stub_pull(monkeypatch, tmp_path)

    for _ in range(2):
        r = client.post("/api/mr/ingest-sheet", json={})
        assert r.status_code == 200 and r.json()["status"] == "ok", r.text
    assert len(fetches) == 2


def test_a_bad_cooldown_setting_falls_back_to_the_default_never_to_off(monkeypatch):
    """A typo in a deployment's env must not become "no gate" or, worse, "never
    pull again"."""
    default = mr_router._DEFAULT_PULL_COOLDOWN_SECONDS
    monkeypatch.delenv("MR_PULL_COOLDOWN_SECONDS", raising=False)
    assert mr_router._pull_cooldown_seconds() == default

    for bad in ("", "  ", "soon", "-5", "inf", "nan"):
        monkeypatch.setenv("MR_PULL_COOLDOWN_SECONDS", bad)
        assert mr_router._pull_cooldown_seconds() == default, bad

    monkeypatch.setenv("MR_PULL_COOLDOWN_SECONDS", "30")
    assert mr_router._pull_cooldown_seconds() == 30.0
    monkeypatch.setenv("MR_PULL_COOLDOWN_SECONDS", "0")
    assert mr_router._pull_cooldown_seconds() == 0.0


def test_a_forced_pull_bypasses_the_cooldown_but_not_the_lock(
        monkeypatch, tmp_path, as_caller):
    """An admin's ``force`` skips the COOLDOWN. It never skips the in-flight lock.
    (The floor is switched off here so the cooldown is the only thing under
    test; the floor has its own tests below.)"""
    _shared_ws(monkeypatch)
    monkeypatch.setenv("MR_FORCE_PULL_FLOOR_SECONDS", "0")
    fetches = _stub_pull(monkeypatch, tmp_path)
    as_caller(ADMIN)
    assert client.post("/api/mr/ingest-sheet", json={}).status_code == 200

    forced = client.post("/api/mr/ingest-sheet", json={"force": True})
    assert forced.status_code == 200 and forced.json()["status"] == "ok", forced.text
    assert len(fetches) == 2

    # "false" is a non-empty string, and so truthy. A flag that skips a safety
    # check only ever fires on a real ``true``.
    lazy = client.post("/api/mr/ingest-sheet", json={"force": "false"})
    assert lazy.json()["status"] == "fresh"
    assert len(fetches) == 2

    # Force is not a licence to overlap: the lock is a different protection from
    # the cooldown and a forced pull still cannot interleave with a running one.
    lock = mr_router._pull_lock(WORKSPACE)
    assert lock.acquire(blocking=False)
    try:
        blocked = client.post("/api/mr/ingest-sheet", json={"force": True})
        assert blocked.status_code == 409, blocked.text
        assert "already running" in blocked.json()["detail"]
    finally:
        lock.release()
    assert len(fetches) == 2


#: ``_FORCE_REFUSED`` / ``_SINGLE_TAB_REFUSED`` / ``_UPLOAD_REFUSED`` in the router
#: — pinned verbatim so the wording the console shows cannot drift from the
#: wording the router raises.
FORCE_REFUSED = ("Only an admin can force a pull. The team's data refreshes on its own "
                 "and a normal pull is always available.")
SINGLE_TAB_REFUSED = ("Pulling a single tab changes the whole team's dashboard, so only an "
                      "admin can do it. A normal pull refreshes every tab.")
UPLOAD_REFUSED = "Uploads go to the whole team's dashboard, so only an admin can add them."

CREATOR = {"id": "u-creator", "email": "creator@legalsoft.com", "is_admin": False,
           "is_creator": True, "session_id": "", "timezone": "UTC"}


def test_a_member_cannot_force_a_pull_but_a_plain_pull_still_works(
        monkeypatch, tmp_path, as_caller):
    """``force`` is the only thing that skips the pull limiter, and the console
    offers it to every reader. So a member is refused 403 — BEFORE the store, the
    lock or Google are touched — with a plain reason; their ordinary pull still
    works, and inside the cooldown still answers ``fresh``."""
    _shared_ws(monkeypatch)
    fetches = _stub_pull(monkeypatch, tmp_path)
    calls = _count_run_reads(monkeypatch)

    as_caller(MEMBER)
    refused = client.post("/api/mr/ingest-sheet", json={"force": True})
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"] == FORCE_REFUSED
    assert fetches == [] and calls == [], (
        "a member's force reached the store or Google before it was refused")

    plain = client.post("/api/mr/ingest-sheet", json={})
    assert plain.status_code == 200 and plain.json()["status"] == "ok", plain.text
    assert len(fetches) == 1
    again = client.post("/api/mr/ingest-sheet", json={})
    assert again.status_code == 200 and again.json()["status"] == "fresh", again.text

    # 403 also when the answer would have been "fresh" anyway: the refusal is
    # about the caller, and it is evaluated before the floor is ever looked at.
    still = client.post("/api/mr/ingest-sheet", json={"force": True})
    assert still.status_code == 403 and "Retry-After" not in still.headers
    assert len(fetches) == 1


def test_a_forced_pull_inside_the_floor_is_a_429_with_retry_after(
        monkeypatch, tmp_path, as_caller):
    """An admin's force skips the cooldown, not the FLOOR. Inside it the pull is
    refused outright — 429 and a ``Retry-After`` — so a scripted caller backs off
    honestly instead of being told the data is fine. A plain pull inside the
    cooldown keeps answering 200 ``fresh``."""
    from marketing_research_agent import runs as mr_runs

    _shared_ws(monkeypatch)
    fetches = _stub_pull(monkeypatch, tmp_path)
    for who in (ADMIN, CREATOR):                   # both roles may force
        as_caller(who)
        if who is ADMIN:
            assert client.post("/api/mr/ingest-sheet", json={}).status_code == 200
        before = {r["id"] for r in mr_runs.list_runs(WORKSPACE)}
        n = len(fetches)

        hammered = client.post("/api/mr/ingest-sheet", json={"force": True})
        assert hammered.status_code == 429, f"{who['id']}: {hammered.text}"
        retry = int(hammered.headers["Retry-After"])
        assert 1 <= retry <= 30, retry
        assert "seconds" in hammered.json()["detail"]
        assert hammered.json().get("status") != "fresh"
        assert len(fetches) == n, "a forced pull inside the floor still fetched"
        assert {r["id"] for r in mr_runs.list_runs(WORKSPACE)} == before

        as_caller(MEMBER)                          # a plain pull is still just "fresh"
        plain = client.post("/api/mr/ingest-sheet", json={})
        assert plain.status_code == 200 and plain.json()["status"] == "fresh"


def test_a_forced_pull_past_the_floor_proceeds(monkeypatch, tmp_path, as_caller):
    """Past the floor the force is honoured — that is what force is for. A pull
    60s old is inside the 120s cooldown (a plain pull is "fresh") and past the
    30s floor (a forced one runs)."""
    _shared_ws(monkeypatch)
    fetches = _stub_pull(monkeypatch, tmp_path)
    _seed_previous_pull(WORKSPACE, stamp=_ago(60))

    as_caller(MEMBER)
    assert client.post("/api/mr/ingest-sheet", json={}).json()["status"] == "fresh"
    assert fetches == []

    as_caller(ADMIN)
    forced = client.post("/api/mr/ingest-sheet", json={"force": True})
    assert forced.status_code == 200 and forced.json()["status"] == "ok", forced.text
    assert len(fetches) == 1


def test_the_lock_is_taken_before_the_floor_is_read(monkeypatch, tmp_path, as_caller):
    """Force never skips the lock, and the lock answers first: with a pull in
    flight AND a very recent one, the admin gets the 409, not the 429."""
    _shared_ws(monkeypatch)
    _stub_pull(monkeypatch, tmp_path)
    _seed_previous_pull(WORKSPACE, stamp=_ago(5))

    lock = mr_router._pull_lock(WORKSPACE)
    assert lock.acquire(blocking=False)
    try:
        as_caller(ADMIN)
        r = client.post("/api/mr/ingest-sheet", json={"force": True})
        assert r.status_code == 409, r.text
    finally:
        lock.release()


def test_the_floor_can_be_switched_off_and_a_bad_setting_never_turns_it_off(
        monkeypatch, tmp_path, as_caller):
    _shared_ws(monkeypatch)
    monkeypatch.setenv("MR_FORCE_PULL_FLOOR_SECONDS", "0")
    fetches = _stub_pull(monkeypatch, tmp_path)
    as_caller(ADMIN)
    for _ in range(2):
        r = client.post("/api/mr/ingest-sheet", json={"force": True})
        assert r.status_code == 200 and r.json()["status"] == "ok", r.text
    assert len(fetches) == 2

    default = mr_router._DEFAULT_FORCE_PULL_FLOOR_SECONDS
    assert default == 30.0
    monkeypatch.delenv("MR_FORCE_PULL_FLOOR_SECONDS")
    assert mr_router._force_pull_floor_seconds() == default
    for bad in ("", "  ", "soon", "-5", "inf", "nan"):
        monkeypatch.setenv("MR_FORCE_PULL_FLOOR_SECONDS", bad)
        assert mr_router._force_pull_floor_seconds() == default, bad
    monkeypatch.setenv("MR_FORCE_PULL_FLOOR_SECONDS", "10")
    assert mr_router._force_pull_floor_seconds() == 10.0


def test_force_and_the_floor_do_nothing_when_the_workspace_is_not_shared(
        monkeypatch, tmp_path):
    """Unshared is today's behaviour, byte for byte: any caller may send
    ``force`` (it has nothing to skip), and there is no floor to hit."""
    fetches = _stub_pull(monkeypatch, tmp_path)
    for _ in range(3):
        r = client.post("/api/mr/ingest-sheet", json={"force": True})   # USER: no admin flags
        assert r.status_code == 200 and r.json()["status"] == "ok", r.text
    assert len(fetches) == 3


def test_the_pull_lock_is_the_workspaces_not_the_members(monkeypatch, tmp_path, as_caller):
    """With one key for everyone, two members pulling at once must serialise. A
    lock keyed on the caller would let them interleave their write and delete
    passes over the SAME rows — the data-loss race the lock exists to prevent."""
    _shared_ws(monkeypatch)
    _stub_pull(monkeypatch, tmp_path)

    lock = mr_router._pull_lock(WORKSPACE)
    assert lock.acquire(blocking=False)
    try:
        for who in (USER, MEMBER):
            as_caller(who)
            r = client.post("/api/mr/ingest-sheet", json={})
            assert r.status_code == 409, f"{who['id']}: {r.status_code} {r.text}"
    finally:
        lock.release()


def test_a_cooldown_skip_is_not_a_degraded_cron(monkeypatch, tmp_path):
    """The scheduler reads only the status code, and a skip is a healthy answer:
    the data is minutes old. Reporting it as 207 would page nobody usefully and
    teach whoever reads the alerts to ignore them."""
    _shared_ws(monkeypatch)
    fetches = _stub_pull(monkeypatch, tmp_path)
    _stub_cron_extras(monkeypatch)

    assert client.post("/api/mr/ingest-sheet", json={}).status_code == 200  # a member, just now
    fired = client.post("/api/mr/cron/refresh", headers={"x-cron-key": "s3cret"})
    assert fired.status_code == 200, fired.text
    body = fired.json()
    assert body["status"] == "ok" and body["errors"] == []
    assert body["pull"]["status"] == "fresh"
    assert len(fetches) == 1


def test_a_duplicate_tracker_dataset_is_swept_by_the_next_pull(monkeypatch, tmp_path):
    """Two Cloud Run instances can pull the same workspace at once, and each swap
    only retires what it read BEFORE it wrote — so both can leave a run of the
    same tab behind. Readers never see the duplicate, but they would pile up.

    Simulated exactly where that race lands: a second run of the same tab is
    written AFTER this pull read its superseded set, so the swap cannot retire it
    and only the sweep can."""
    from marketing_research_agent import runs as mr_runs

    _shared_ws(monkeypatch)
    _stub_pull(monkeypatch, tmp_path)
    real_save = mr_runs.save_run
    strays: list[str] = []

    def _save_with_a_racing_twin(run):
        durable = real_save(run)
        if run.get("kind") == "dataset" and run.get("platform") == "sheets:Vendor A":
            stray = {**run, "id": mr_runs.new_run_id(),
                     "generated_at": "2026-01-01T00:00:00+00:00"}  # the older twin
            real_save(stray)
            strays.append(stray["id"])
        return durable

    monkeypatch.setattr(mr_runs, "save_run", _save_with_a_racing_twin)

    r = client.post("/api/mr/ingest-sheet", json={})
    assert r.status_code == 200, r.text
    assert len(strays) == 1
    survivors = mr_runs.list_runs(WORKSPACE, kind="dataset")
    assert [s["platform"] for s in survivors] == ["sheets:Vendor A"], (
        "the twin survived the pull that should have swept it")
    assert strays[0] not in {s["id"] for s in survivors}
    assert survivors[0]["generated_at"] > "2026-06", "the sweep kept the wrong copy"


def test_the_sweep_keeps_the_newest_copy_of_a_tab_and_never_touches_an_upload(
        monkeypatch, tmp_path):
    """The invariant that makes the sweep safe: per tab the NEWEST run always
    survives, so it can never empty a tab — and only ``sheets:*`` is ever a
    candidate, because an upload is somebody's contribution and its "duplicates"
    (two ``google_ads`` files) are both wanted."""
    from marketing_research_agent import runs as mr_runs

    def _dataset(platform, stamp, who=WORKSPACE):
        rid = mr_runs.new_run_id()
        mr_runs.save_run({"id": rid, "kind": "dataset", "user_id": who, "agent_id": "a6",
                          "platform": platform, "generated_at": stamp,
                          "metrics": [], "leads": [], "gaps": []})
        return rid

    a_old = _dataset("sheets:Vendor A", "2026-07-01T00:00:00+00:00")
    a_mid = _dataset("sheets:Vendor A", "2026-07-02T00:00:00+00:00")
    a_new = _dataset("sheets:Vendor A", "2026-07-03T00:00:00+00:00")
    b_only = _dataset("sheets:Vendor B", "2026-07-01T00:00:00+00:00")
    up_1 = _dataset("google_ads", "2026-07-01T00:00:00+00:00")
    up_2 = _dataset("google_ads", "2026-07-02T00:00:00+00:00")
    pdf = _dataset("pdf:board.pdf", "2026-07-01T00:00:00+00:00")
    elsewhere = _dataset("sheets:Vendor A", "2026-06-01T00:00:00+00:00", who="someone-else")

    assert mr_router._sweep_superseded_tracker_runs(WORKSPACE) == 2

    def alive(rid):
        return mr_runs.get_run(rid) is not None

    assert (alive(a_new), alive(a_mid), alive(a_old)) == (True, False, False)
    assert alive(b_only) and alive(up_1) and alive(up_2) and alive(pdf)
    assert alive(elsewhere), "the sweep crossed into another workspace"
    assert mr_router._sweep_superseded_tracker_runs(WORKSPACE) == 0  # and it is idempotent


def test_an_unreadable_store_during_the_freshness_check_still_answers_502(
        monkeypatch, tmp_path):
    """The freshness gate reads the store, and a store that cannot be read must
    not read as "nothing pulled recently, go ahead" NOR as "fresh". It falls
    through, and the pull's own read of the existing runs is what answers — the
    same honest 502 the rest of the pull contract gives."""
    _shared_ws(monkeypatch)
    _stub_pull(monkeypatch, tmp_path)
    _dead_runs_store(monkeypatch)

    r = client.post("/api/mr/ingest-sheet", json={})
    assert r.status_code == 502, r.text
    detail = r.json()["detail"]
    assert "left untouched" in detail and "could not read the existing runs" in detail


def test_with_no_workspace_key_every_read_is_still_the_callers_own(
        monkeypatch, tmp_path, as_caller):
    """The parity gate for local, dev and every suite above: with neither
    ``MR_CRON_USER_ID`` nor ``MR_WORKSPACE_ID`` set, nothing about the agent
    changes — a pull belongs to whoever pressed it and nobody else sees it —
    and there is no cooldown, because there is no shared key to protect."""
    from marketing_research_agent import runs as mr_runs

    fetches = _stub_pull(monkeypatch, tmp_path)
    assert client.post("/api/mr/ingest-sheet", json={}).status_code == 200
    assert mr_runs.list_runs(USER["id"], kind="dataset"), "the pull was not the caller's own"

    as_caller(MEMBER)
    assert client.get("/api/mr/overview").json()["has_data"] is False
    assert client.get("/api/mr/datasets").json() == []
    assert client.get("/api/mr/lead-analysis").json()["has_data"] is False
    assert client.get("/api/mr/report-periods").json()["months"] == []

    as_caller(USER)
    again = client.post("/api/mr/ingest-sheet", json={})
    assert again.status_code == 200 and again.json()["status"] == "ok", again.text
    assert len(fetches) == 2, "an unshared workspace was rate-limited by a cooldown"


def test_the_kill_switch_returns_the_agent_to_per_user_reads(monkeypatch, tmp_path, as_caller):
    """Turning sharing off — ``MR_WORKSPACE_SHARED=0`` — is the rollback, with the
    key still configured. No data moves: a pull is stamped with the caller again,
    and a member reads only their own — exactly the mode above. Unsetting the
    variable is the same thing (see the next tests)."""
    from marketing_research_agent import runs as mr_runs

    _shared_ws(monkeypatch)
    monkeypatch.setenv("MR_WORKSPACE_SHARED", "0")
    fetches = _stub_pull(monkeypatch, tmp_path)

    assert client.post("/api/mr/ingest-sheet", json={}).status_code == 200
    assert mr_runs.list_runs(WORKSPACE) == []
    assert mr_runs.list_runs(USER["id"], kind="dataset")

    as_caller(MEMBER)
    assert client.get("/api/mr/overview").json()["has_data"] is False

    as_caller(USER)                       # and no cooldown either
    assert client.post("/api/mr/ingest-sheet", json={}).json()["status"] == "ok"
    assert len(fetches) == 2

    monkeypatch.setenv("MR_WORKSPACE_SHARED", "1")   # switched ON: the workspace's key
    as_caller(MEMBER)
    assert client.get("/api/mr/overview").json()["has_data"] is False  # nothing under it yet
    assert client.get("/api/mr/datasets").json() == []


def test_deploying_without_the_switch_changes_nothing(monkeypatch, tmp_path, as_caller):
    """Production ALREADY has ``MR_CRON_USER_ID`` set. Shipping the shared
    workspace must therefore not, by itself, share anything: key configured,
    ``MR_WORKSPACE_SHARED`` unset -> every read and write is still the caller's
    own, the cron still pulls for the cron account, and the new restrictions
    (admin-only uploads, force, single-tab pulls) are not in force — because
    there is nothing shared for them to protect. Enabling sharing is one
    deliberate env change."""
    from marketing_research_agent import runs as mr_runs

    monkeypatch.setenv("MR_CRON_USER_ID", WORKSPACE)          # the key — and no switch
    monkeypatch.setenv("MR_WORKSPACE_ID", "another-workspace")
    fetches = _stub_pull(monkeypatch, tmp_path)
    _stub_cron_extras(monkeypatch)

    assert client.post("/api/mr/ingest-sheet", json={}).status_code == 200
    assert mr_runs.list_runs(USER["id"], kind="dataset"), "the pull was not the caller's own"
    assert mr_runs.list_runs(WORKSPACE) == [] and mr_runs.list_runs("another-workspace") == []

    as_caller(MEMBER)
    assert client.get("/api/mr/overview").json()["has_data"] is False
    assert client.get("/api/mr/datasets").json() == []
    assert client.get("/api/mr/report-periods").json()["months"] == []

    # A member is not restricted: nothing is shared, so what they write is theirs.
    up = client.post("/api/mr/ingest", files={"file": ("g.csv", io.BytesIO(CSV), "text/csv")},
                     data={"platform": "google_ads"})
    assert up.status_code == 200, up.text
    assert mr_runs.get_run(up.json()["dataset_id"])["user_id"] == MEMBER["id"]
    assert client.post("/api/mr/ingest-sheet", json={"force": True}).status_code == 200
    assert len(fetches) == 2

    # The cron is what it was: it pulls for the configured account, whose own
    # sign-in is the only one that reads it.
    fired = client.post("/api/mr/cron/refresh", headers={"x-cron-key": "s3cret"})
    assert fired.status_code == 200, fired.text
    assert mr_runs.list_runs(WORKSPACE, kind="dataset")
    as_caller({**MEMBER, "id": "u3"})
    assert client.get("/api/mr/overview").json()["has_data"] is False


def test_a_typo_in_the_switch_fails_closed(monkeypatch, tmp_path, as_caller):
    """A switch that only recognised "0"/"false"/"off" as off would leave sharing
    ON for ``ture``. Here only 1/true/yes/on turn it on, so every other value —
    a typo made enabling it, or disabling it — is OFF, at the HTTP surface too."""
    from marketing_research_agent import runs as mr_runs

    fetches = _stub_pull(monkeypatch, tmp_path)
    monkeypatch.setenv("MR_CRON_USER_ID", WORKSPACE)
    for typo in ("ture", "enabled", "2", "y", "of"):
        monkeypatch.setenv("MR_WORKSPACE_SHARED", typo)
        as_caller(USER)
        assert client.post("/api/mr/ingest-sheet", json={"force": True}).status_code == 200
        assert mr_runs.list_runs(WORKSPACE) == [], f"{typo!r} shared the workspace"
        as_caller(MEMBER)
        assert client.get("/api/mr/overview").json()["has_data"] is False, typo
    assert len(fetches) == 5


def test_an_admins_upload_and_single_tab_pull_land_under_the_workspace_key(
        monkeypatch, tmp_path, as_caller):
    """The other write sites. Each is stamped with the WORKSPACE key (so every
    member reads it) and with who added it, server-derived. Only an admin gets
    this far while the workspace is shared."""
    from pypdf import PdfWriter

    from marketing_research_agent import runs as mr_runs

    _shared_ws(monkeypatch)
    monkeypatch.setenv("MR_PULL_COOLDOWN_SECONDS", "0")
    _stub_single_tab(monkeypatch)

    as_caller(ADMIN)
    csv = client.post("/api/mr/ingest", files={"file": ("g.csv", io.BytesIO(CSV), "text/csv")},
                      data={"platform": "google_ads", "created_by": "someone-else"})
    buf = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(buf)
    pdf = client.post("/api/mr/ingest-pdf",
                      files={"file": ("board.pdf", io.BytesIO(buf.getvalue()), "application/pdf")})
    tab = client.post("/api/mr/ingest-sheet", json={"gid": "1"})
    assert (csv.status_code, pdf.status_code, tab.status_code) == (200, 200, 200), (
        csv.text, pdf.text, tab.text)

    stored = {r["platform"]: r for r in mr_runs.list_runs(WORKSPACE, kind="dataset")}
    assert set(stored) == {"google_ads", "pdf:board.pdf", "sheets:Vendor A"}
    assert mr_runs.list_runs(ADMIN["id"]) == [], "a write was stamped with the caller"
    for platform in stored:
        assert stored[platform]["created_by"] == ADMIN["id"], (
            f"{platform}: created_by must be the authenticated caller, never the request")

    as_caller(USER)
    assert {d["platform"] for d in client.get("/api/mr/datasets").json()} == set(stored)


def test_the_runs_list_costs_two_scoped_queries_when_the_workspace_is_shared(
        monkeypatch, as_caller):
    """Companion to ``test_the_runs_list_asks_for_report_kinds_only``, which pins
    ONE query in the unshared mode. Shared, a member's Reports list is two
    equality queries — their own runs under their id, the board kinds under the
    workspace key — never a scan, and never the workspace's non-board runs."""
    from marketing_research_agent import reports

    _shared_ws(monkeypatch)
    as_caller(MEMBER)
    calls = _count_run_reads(monkeypatch)
    assert client.get("/api/mr/runs").status_code == 200

    assert len(calls) == 2, calls
    (own_id, own_kinds), (ws_id, ws_kinds) = calls
    assert own_id == MEMBER["id"] and "daily_summary" in own_kinds
    assert ws_id == WORKSPACE and set(ws_kinds) == set(reports.BOARD_KINDS), (
        "the workspace query must ask for the board kinds only — every other kind "
        "under that key is somebody else's private report")

    # The caller who IS the workspace key needs no second query.
    monkeypatch.setenv("MR_CRON_USER_ID", MEMBER["id"])
    calls.clear()
    assert client.get("/api/mr/runs").status_code == 200
    assert len(calls) == 1, calls


def test_the_shared_runs_list_merges_newest_first_and_keeps_private_reports_private(
        monkeypatch, as_caller):
    """One list, two sources: the caller's own reports and the workspace's board
    reports, ordered together. Another member's private report is in neither."""
    from marketing_research_agent import runs as mr_runs

    _shared_ws(monkeypatch)

    def _run(rid, kind, owner, stamp):
        mr_runs.save_run({"id": rid, "kind": kind, "user_id": owner, "agent_id": "a6",
                          "generated_at": stamp, "structured": {}})

    _run("mine-old", "daily_summary", MEMBER["id"], "2026-09-01T00:00:00+00:00")
    _run("board-mid", "board_report", WORKSPACE, "2026-09-02T00:00:00+00:00")
    _run("mine-new", "weekly_summary", MEMBER["id"], "2026-09-03T00:00:00+00:00")
    _run("theirs", "daily_summary", USER["id"], "2026-09-04T00:00:00+00:00")
    _run("ws-narrated", "daily_summary", WORKSPACE, "2026-09-05T00:00:00+00:00")

    as_caller(MEMBER)
    listed = [r["id"] for r in client.get("/api/mr/runs").json()]
    assert listed == ["mine-new", "board-mid", "mine-old"], listed

def test_the_shared_lead_summary_is_flagged_against_the_workspaces_targets(
        monkeypatch, tmp_path, as_caller):
    """The lead-quality flags are frozen into the run the WHOLE workspace reads,
    so they are judged against ``targets__{workspace}`` — never against whichever
    member happened to press Pull. Recorded at the one place targets are read
    during a pull: a member's pull must only ever ask for the workspace's."""
    from marketing_research_agent import goals as mr_goals

    _shared_ws(monkeypatch)
    _stub_pull(monkeypatch, tmp_path, lead_tab=_lead_tab())
    asked: list = []
    real = mr_goals.get_targets
    monkeypatch.setattr(mr_goals, "get_targets", lambda uid: asked.append(uid) or real(uid))

    as_caller(MEMBER)
    r = client.post("/api/mr/ingest-sheet", json={})
    assert r.status_code == 200, r.text
    assert asked, "the pull never consulted any targets, so this proves nothing"
    assert set(asked) == {WORKSPACE}, (
        f"the shared lead summary was judged against {sorted(set(asked))}")


# --- uploads: an admin's while the workspace is shared ------------------------
# While shared an upload joins the ONE dashboard the team reads, replaces that
# platform's figure for everyone (newest per platform wins) and is permanent
# (``dataset`` is exempt from retention; the swap and the sweep only touch
# ``sheets:*``). A member could therefore overwrite a figure for everybody, and
# then could not even delete what they had written. Admin-only removes both.

def _blank_pdf() -> bytes:
    from pypdf import PdfWriter

    buf = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    writer.write(buf)
    return buf.getvalue()


def test_a_member_cannot_upload_while_the_workspace_is_shared(monkeypatch, as_caller):
    from marketing_research_agent import runs as mr_runs

    _shared_ws(monkeypatch)
    as_caller(MEMBER)

    csv = client.post("/api/mr/ingest", files={"file": ("g.csv", io.BytesIO(CSV), "text/csv")},
                      data={"platform": "google_ads"})
    pdf = client.post("/api/mr/ingest-pdf",
                      files={"file": ("board.pdf", io.BytesIO(_blank_pdf()), "application/pdf")})
    for r in (csv, pdf):
        assert r.status_code == 403, r.text
        assert r.json()["detail"] == UPLOAD_REFUSED
    assert mr_runs.list_runs(WORKSPACE) == [] and mr_runs.list_runs(MEMBER["id"]) == [], (
        "a refused upload was stored anyway")

    # The refusal comes FIRST — ahead of validation — so it depends on nothing
    # else in the request and a member learns nothing by probing it.
    bad_platform = client.post("/api/mr/ingest",
                               files={"file": ("g.csv", io.BytesIO(CSV), "text/csv")},
                               data={"platform": "no-such-platform"})
    not_a_pdf = client.post("/api/mr/ingest-pdf",
                            files={"file": ("notes.txt", io.BytesIO(b"hi"), "text/plain")})
    assert (bad_platform.status_code, not_a_pdf.status_code) == (403, 403)


def test_an_admin_or_creator_can_upload_while_shared_and_is_recorded_as_the_author(
        monkeypatch, as_caller):
    from marketing_research_agent import runs as mr_runs

    _shared_ws(monkeypatch)
    for who, name in ((ADMIN, "admin.pdf"), (CREATOR, "creator.pdf")):
        as_caller(who)
        csv = client.post("/api/mr/ingest",
                          files={"file": ("g.csv", io.BytesIO(CSV), "text/csv")},
                          data={"platform": "google_ads"})
        pdf = client.post("/api/mr/ingest-pdf",
                          files={"file": (name, io.BytesIO(_blank_pdf()), "application/pdf")})
        assert (csv.status_code, pdf.status_code) == (200, 200), (csv.text, pdf.text)
        for r in (mr_runs.get_run(csv.json()["dataset_id"]),
                  mr_runs.get_run(pdf.json()["dataset_id"])):
            assert r["user_id"] == WORKSPACE and r["created_by"] == who["id"]


def test_uploads_stay_open_to_everyone_when_the_workspace_is_not_shared(monkeypatch, as_caller):
    """Unshared the upload is the caller's own — nobody else reads it — so
    nothing about it changed."""
    from marketing_research_agent import runs as mr_runs

    as_caller(MEMBER)
    csv = client.post("/api/mr/ingest", files={"file": ("g.csv", io.BytesIO(CSV), "text/csv")},
                      data={"platform": "google_ads"})
    pdf = client.post("/api/mr/ingest-pdf",
                      files={"file": ("board.pdf", io.BytesIO(_blank_pdf()), "application/pdf")})
    assert (csv.status_code, pdf.status_code) == (200, 200), (csv.text, pdf.text)
    assert mr_runs.get_run(csv.json()["dataset_id"])["user_id"] == MEMBER["id"]


# --- the single-tab (gid) pull: an admin's, on the tab's own key --------------
# It used to file a tab under ``sheets:<gid>`` with the workspace key. The read
# path keeps the newest run per PLATFORM, so the same tab counted twice (once
# under its title, once under its number); ``is_rollup_title`` matches "overall"
# in the LABEL, so a numeric label walked the roll-up straight past the guard;
# and the branch had no lock and no limiter at all. No member-facing screen sends
# a ``gid`` — the two callers of ``mrIngestSheet`` send ``{year}`` and
# ``{force: true}`` — so restricting it to admins costs no one anything.

def test_a_member_cannot_pull_a_single_tab_while_shared(monkeypatch, tmp_path, as_caller):
    from marketing_research_agent import runs as mr_runs

    _shared_ws(monkeypatch)
    fetches = _stub_pull(monkeypatch, tmp_path)
    calls = _stub_single_tab(monkeypatch)
    reads = _count_run_reads(monkeypatch)

    as_caller(MEMBER)
    for gid in ("1", 1, "2"):
        r = client.post("/api/mr/ingest-sheet", json={"gid": gid})
        assert r.status_code == 403, f"{gid!r}: {r.text}"
        assert r.json()["detail"] == SINGLE_TAB_REFUSED
    assert calls == [] and fetches == [] and reads == [], (
        "a member's single-tab request reached the store or Google before it was refused")
    assert mr_runs.list_runs(WORKSPACE) == []


def test_a_single_tab_pull_lands_on_the_tabs_own_key_and_supersedes_the_pull(
        monkeypatch, tmp_path, as_caller):
    """The double count, closed. After a full pull the workspace holds
    ``sheets:Vendor A``. An admin's single-tab pull of that tab is filed under
    the SAME key — by title, not number — so it replaces the row instead of
    adding a second vendor, and the tab is counted once."""
    _shared_ws(monkeypatch)
    monkeypatch.setenv("MR_FORCE_PULL_FLOOR_SECONDS", "0")
    _stub_pull(monkeypatch, tmp_path)
    calls = _stub_single_tab(monkeypatch)
    as_caller(ADMIN)
    assert client.post("/api/mr/ingest-sheet", json={}).status_code == 200
    first = client.get("/api/mr/datasets").json()
    assert [d["platform"] for d in first] == ["sheets:Vendor A"] and first[0]["metrics"] == 1

    r = client.post("/api/mr/ingest-sheet", json={"gid": "1", "force": True})
    assert r.status_code == 200, r.text
    assert r.json()["status"] == "ok" and r.json()["tabs"][0]["tab"] == "Vendor A"
    assert ("meta", mr_router.mr_config.SHEETS_SPREADSHEET_ID) in calls

    rows = client.get("/api/mr/datasets").json()
    assert [d["platform"] for d in rows] == ["sheets:Vendor A"], (
        f"the tab is listed under more than one key: {[d['platform'] for d in rows]}")
    assert rows[0]["metrics"] == 2                       # the single-tab run's June + July
    assert rows[0]["created_by"] == ADMIN["id"] and rows[0]["superseded"] is False
    assert rows[0]["id"] != first[0]["id"], "the pull's own row was not superseded"
    assert not [d for d in rows if d["platform"] == "sheets:1"], "a gid-keyed vendor appeared"

    # Counted ONCE: only the newest run of the tab feeds the workspace's figures.
    assert len(mr_router._load_dataset(WORKSPACE)["metrics"]) == 2


def test_a_single_tab_pull_refuses_the_rollup_tab(monkeypatch, tmp_path, as_caller):
    """Its numbers are the sum of the vendor tabs. Refused at write time by
    NAME (the "Overall" tab) and by SCOPE (a tab whose A1 dropdown was left on
    "All"), 422, and nothing is written."""
    from marketing_research_agent import runs as mr_runs

    _shared_ws(monkeypatch)
    monkeypatch.setenv("MR_PULL_COOLDOWN_SECONDS", "0")
    tabs = [{"gid": 2, "title": "Marketing 2026 Overall Report", "hidden": False},
            {"gid": 5, "title": "Vendor C", "hidden": False}]
    _stub_single_tab(monkeypatch, tabs=tabs, values={
        "Vendor C": [["All", "Jun (Performance)"], ["Spend", "$9,999.00"], ["Leads", "9"]]})
    as_caller(ADMIN)

    for gid in ("2", "5"):
        r = client.post("/api/mr/ingest-sheet", json={"gid": gid})
        assert r.status_code == 422, f"gid {gid}: {r.text}"
        assert "roll-up" in r.json()["detail"]
    assert mr_runs.list_runs(WORKSPACE) == [], "a refused roll-up was written anyway"


def test_a_single_tab_pull_refuses_a_hidden_unknown_or_unreadable_tab_and_writes_nothing(
        monkeypatch, tmp_path, as_caller):
    from marketing_research_agent import runs as mr_runs

    _shared_ws(monkeypatch)
    monkeypatch.setenv("MR_PULL_COOLDOWN_SECONDS", "0")
    good = _seed_previous_pull(WORKSPACE)             # holds a good ``sheets:Vendor A``
    _stub_single_tab(monkeypatch, values={"Vendor B": [["Vendor B"], ["Notes", "nothing"]]})
    as_caller(ADMIN)

    hidden = client.post("/api/mr/ingest-sheet", json={"gid": "3"})
    assert hidden.status_code == 422 and "hidden" in hidden.json()["detail"], hidden.text
    unknown = client.post("/api/mr/ingest-sheet", json={"gid": "999"})
    assert unknown.status_code == 404, unknown.text
    empty = client.post("/api/mr/ingest-sheet", json={"gid": "4"})     # parses to nothing
    assert empty.status_code == 422 and "nothing was changed" in empty.json()["detail"], empty.text

    assert {r["id"] for r in mr_runs.list_runs(WORKSPACE)} == set(good.values()), (
        "a refused single-tab pull wrote or deleted a run — an EMPTY run under a "
        "tab's title would supersede the good one and blank that vendor for everyone")


def test_a_single_tab_pull_that_cannot_read_the_workbook_is_a_502_never_a_fallback_label(
        monkeypatch, tmp_path, as_caller):
    from marketing_research_agent import runs as mr_runs

    _shared_ws(monkeypatch)
    monkeypatch.setenv("MR_PULL_COOLDOWN_SECONDS", "0")
    as_caller(ADMIN)

    def _down(*a, **kw):
        raise RuntimeError("429 Too Many Requests")

    monkeypatch.setattr(mr_router, "workbook_meta", _down)          # the tab list
    listing = client.post("/api/mr/ingest-sheet", json={"gid": "1"})
    assert listing.status_code == 502, listing.text
    assert "left untouched" in listing.json()["detail"] and "429" in listing.json()["detail"]

    _stub_single_tab(monkeypatch)
    monkeypatch.setattr(mr_router, "fetch_tab_values", _down)       # the tab itself
    values = client.post("/api/mr/ingest-sheet", json={"gid": "1"})
    assert values.status_code == 502 and "429" in values.json()["detail"], values.text
    assert mr_runs.list_runs(WORKSPACE) == [], "a failed read still wrote a run"


def test_a_single_tab_pull_takes_the_same_lock_cooldown_and_floor_as_the_full_pull(
        monkeypatch, tmp_path, as_caller):
    _shared_ws(monkeypatch)
    _stub_pull(monkeypatch, tmp_path)
    calls = _stub_single_tab(monkeypatch)
    as_caller(ADMIN)

    # The lock — held by a pull in flight.
    lock = mr_router._pull_lock(WORKSPACE)
    assert lock.acquire(blocking=False)
    try:
        busy = client.post("/api/mr/ingest-sheet", json={"gid": "1", "force": True})
        assert busy.status_code == 409, busy.text
    finally:
        lock.release()
    assert calls == []

    # The cooldown — a plain single-tab pull just after a full pull is "fresh".
    assert client.post("/api/mr/ingest-sheet", json={}).status_code == 200
    fresh = client.post("/api/mr/ingest-sheet", json={"gid": "1"})
    assert fresh.status_code == 200 and fresh.json()["status"] == "fresh", fresh.text
    assert fresh.json()["tabs"] == [] and calls == []

    # The floor — force skips the cooldown but not this.
    too_soon = client.post("/api/mr/ingest-sheet", json={"gid": "1", "force": True})
    assert too_soon.status_code == 429 and int(too_soon.headers["Retry-After"]) >= 1
    assert calls == []


def test_unshared_the_single_tab_pull_is_exactly_what_it_was(monkeypatch, as_caller):
    """Unshared the tab is the caller's own and the branch is untouched: any
    caller, the CSV-export source, the ``gid`` as the label, no admin check."""
    from marketing_research_agent import runs as mr_runs

    class _OneTab:
        def __init__(self, *a, **kw):
            pass

        def fetch_campaign_metrics(self, _range):
            return _one_tracker_tab()[0]["metrics"], []

    monkeypatch.setattr(mr_router, "SheetsSource", _OneTab)
    r = client.post("/api/mr/ingest-sheet", json={"gid": "42", "force": True})
    assert r.status_code == 200, r.text
    assert [d["platform"] for d in mr_runs.list_runs(USER["id"], kind="dataset")] == [
        "sheets:42"]


# --- the freshness clock is a FULL pull's, and nothing else's ------------------

def test_a_single_tab_pull_does_not_start_the_freshness_clock(
        monkeypatch, tmp_path, as_caller):
    """The freeze. If a single-tab (or any dataset) run advanced the clock, one
    POST every few seconds would keep the cooldown permanently "fresh": every cron
    fire would answer 200, fetch nothing and sweep nothing, with ``errors: []``,
    while the team's tracker data stood still and nothing alerted. The clock reads
    ``official_spend`` — written by a full pull and by nothing else."""
    _shared_ws(monkeypatch)
    monkeypatch.setenv("MR_FORCE_PULL_FLOOR_SECONDS", "0")
    fetches = _stub_pull(monkeypatch, tmp_path)
    _stub_single_tab(monkeypatch)
    _stub_cron_extras(monkeypatch)
    _seed_previous_pull(WORKSPACE, stamp=_ago(1000))         # the last FULL pull: long ago

    as_caller(ADMIN)
    tab = client.post("/api/mr/ingest-sheet", json={"gid": "1"})
    assert tab.status_code == 200 and tab.json()["status"] == "ok", tab.text
    assert fetches == []

    # ...and right after it, the ordinary pull and the cron both still RUN.
    as_caller(MEMBER)
    plain = client.post("/api/mr/ingest-sheet", json={})
    assert plain.status_code == 200 and plain.json()["status"] == "ok", plain.text
    assert len(fetches) == 1


def test_a_single_tab_pull_never_makes_the_cron_answer_fresh(monkeypatch, tmp_path, as_caller):
    _shared_ws(monkeypatch)
    fetches = _stub_pull(monkeypatch, tmp_path)
    _stub_single_tab(monkeypatch)
    _stub_cron_extras(monkeypatch)
    _seed_previous_pull(WORKSPACE, stamp=_ago(1000))

    as_caller(ADMIN)
    assert client.post("/api/mr/ingest-sheet", json={"gid": "1"}).status_code == 200
    fired = client.post("/api/mr/cron/refresh", headers={"x-cron-key": "s3cret"})
    assert fired.status_code == 200, fired.text
    assert fired.json()["pull"]["status"] == "ok", (
        "a single-tab pull froze the cron: it answered fresh and fetched nothing")
    assert len(fetches) == 1


def test_only_a_full_pull_advances_the_clock(monkeypatch, tmp_path):
    """The clock, read directly: uploads, single-tab and tracker datasets are all
    ignored; only ``official_spend`` counts."""
    from datetime import datetime

    from marketing_research_agent import runs as mr_runs

    assert mr_router._last_pull_at(WORKSPACE) is None
    for platform in ("google_ads", "pdf:x.pdf", "sheets:Vendor A", "sheets:42"):
        mr_runs.save_run({"id": mr_runs.new_run_id(), "kind": "dataset", "user_id": WORKSPACE,
                          "platform": platform, "generated_at": _ago(1), "metrics": []})
    assert mr_router._last_pull_at(WORKSPACE) is None

    stamp = _ago(50)
    mr_runs.save_run({"id": mr_runs.new_run_id(), "kind": "official_spend", "user_id": WORKSPACE,
                      "platform": "sheets-official", "generated_at": stamp, "months": {}})
    assert mr_router._last_pull_at(WORKSPACE) == datetime.fromisoformat(stamp)


# --- the workspace's datasets list says which run still counts ------------------

@pytest.mark.parametrize("shared", [False, True])
def test_the_datasets_list_marks_the_runs_that_no_longer_count(
        monkeypatch, as_caller, shared):
    """``_latest_datasets`` keeps only the newest run per platform, so two
    ``google_ads`` uploads shadow each other and only the later one is on the
    dashboard. The list used to show both as if both counted."""
    from marketing_research_agent import runs as mr_runs

    key = USER["id"]
    if shared:
        key = _shared_ws(monkeypatch)

    def _dataset(platform, stamp):
        rid = mr_runs.new_run_id()
        mr_runs.save_run({"id": rid, "kind": "dataset", "user_id": key, "agent_id": "a6",
                          "platform": platform, "generated_at": stamp,
                          "metrics": [], "leads": [], "gaps": []})
        return rid

    old_up = _dataset("google_ads", "2026-07-01T00:00:00+00:00")
    new_up = _dataset("google_ads", "2026-07-03T00:00:00+00:00")
    mid_up = _dataset("google_ads", "2026-07-02T00:00:00+00:00")
    pdf = _dataset("pdf:board.pdf", "2026-07-01T00:00:00+00:00")
    tab = _dataset("sheets:Vendor A", "2026-07-01T00:00:00+00:00")

    rows = {d["id"]: d for d in client.get("/api/mr/datasets").json()}
    assert {i: rows[i]["superseded"] for i in rows} == {
        new_up: False, mid_up: True, old_up: True, pdf: False, tab: False}

    # It agrees with what the read path actually counts.
    counted = {run["id"] for run in mr_router._latest_datasets(key).values()}
    assert counted == {i for i, d in rows.items() if not d["superseded"]}


# --- the cron's account id is read stripped -------------------------------------

def test_a_whitespace_only_cron_user_id_is_unset_not_a_500(monkeypatch, tmp_path):
    """It used to pass ``if not uid`` and then raise in ``workspace_id`` — an
    unhandled 500 out of a cron endpoint. Blank is UNSET: the existing skipped
    path, a degraded 207, in both modes."""
    _stub_pull(monkeypatch, tmp_path)
    _stub_cron_extras(monkeypatch)
    for shared in (False, True):
        if shared:
            monkeypatch.setenv("MR_WORKSPACE_SHARED", "1")
        for blank in ("   ", "\t", " \n "):
            monkeypatch.setenv("MR_CRON_USER_ID", blank)
            r = client.post("/api/mr/cron/refresh", headers={"x-cron-key": "s3cret"})
            assert r.status_code == 207, f"{blank!r} shared={shared}: {r.status_code} {r.text}"
            assert r.json()["pull"] == "skipped (MR_CRON_USER_ID unset)"
            assert r.json()["status"] == "partial"


def test_a_trailing_space_in_the_cron_user_id_does_not_change_the_key(monkeypatch, tmp_path):
    """Unshared, the cron wrote under the raw value ("abc ") — a key nobody reads
    ("abc") — so a stray space meant a green cron and a blank dashboard."""
    from marketing_research_agent import runs as mr_runs

    _stub_pull(monkeypatch, tmp_path)
    _stub_cron_extras(monkeypatch)
    monkeypatch.setenv("MR_CRON_USER_ID", "cron-account \n")

    r = client.post("/api/mr/cron/refresh", headers={"x-cron-key": "s3cret"})
    assert r.status_code == 200, r.text
    assert mr_runs.list_runs("cron-account", kind="dataset"), "the cron wrote under a padded key"
    stored = {run["user_id"] for run in mr_runs.list_runs("cron-account")}
    assert stored == {"cron-account"}


# --- /mr/ask carries the workspace's official totals ----------------------------
# Ask used to see only the sheet grids, so its headline figure was a vendor-tab
# sum while the dashboard swapped in the Overall tab's own rows. Same question,
# two numbers. The handler now makes ONE scoped read of this workspace's newest
# official-totals run and hands it to the Ask engine.

_ASK_TAB = [["Meta Ads", "Aug (Performance)"], ["Spend", "$4,000"], ["Leads", "40"]]


def _stub_ask_workbook(monkeypatch):
    from marketing_research_agent import profiles as mr_profiles
    from marketing_research_agent.workbook import TabGrid

    grid = TabGrid("Meta Ads", 1, False, _ASK_TAB, len(_ASK_TAB), 2)
    monkeypatch.setattr(mr_router, "_workbook_bundle",
                        lambda **kw: ([grid], [mr_profiles._heuristic_profile(grid, 2026)]))


def _seen_official(monkeypatch) -> list[dict]:
    """Record what the handler passes to the Ask engine, and run the real one."""
    seen: list[dict] = []
    real = mr_router.mr_insight.answer

    def _spy(*a, **kw):
        seen.append(kw.get("official_totals"))
        return real(*a, **kw)

    monkeypatch.setattr(mr_router.mr_insight, "answer", _spy)
    return seen


def _save_official(user_id, totals):
    from datetime import datetime, timezone

    from marketing_research_agent import runs as mr_runs

    mr_runs.save_run({
        "id": mr_runs.new_run_id(), "kind": "official_spend", "user_id": user_id,
        "agent_id": "a6", "platform": "sheets-official",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "months": {k: v["spend"] for k, v in totals.items() if "spend" in v},
        "totals": totals,
    })


def test_ask_passes_this_workspaces_official_totals_to_the_engine(monkeypatch):
    _stub_ask_workbook(monkeypatch)
    seen = _seen_official(monkeypatch)
    _save_official(USER["id"], {"2026-08": {"spend": 6500.0, "leads": 60}})
    _save_official("someone-else", {"2026-08": {"spend": 999999.0}})

    r = client.post("/api/mr/ask", json={"question": "how much did we spend in August 2026"})
    assert r.status_code == 200, r.text
    assert seen == [{"2026-08": {"spend": 6500.0, "leads": 60}}], "another workspace's roll-up leaked in"


def test_ask_reads_the_official_totals_once_and_never_the_whole_dataset(monkeypatch):
    """`_load_dataset` rehydrates ~13 large documents; the question needs one
    scoped read of the newest official-totals run."""
    from marketing_research_agent import runs as mr_runs

    _stub_ask_workbook(monkeypatch)
    _save_official(USER["id"], {"2026-08": {"spend": 6500.0, "leads": 60}})
    calls: list[object] = []
    real_list = mr_runs.list_runs
    monkeypatch.setattr(mr_runs, "list_runs",
                        lambda uid, **kw: (calls.append(kw.get("kind")), real_list(uid, **kw))[1])
    monkeypatch.setattr(mr_router, "_load_dataset",
                        lambda *a, **kw: pytest.fail("/mr/ask loaded the whole dataset"))

    r = client.post("/api/mr/ask", json={"question": "spend in August 2026"})
    assert r.status_code == 200, r.text
    assert calls == ["official_spend"], calls


def test_ask_degrades_to_tracker_sums_when_the_run_store_fails(monkeypatch):
    """A store failure must cost the parity note, never the answer."""
    from marketing_research_agent import runs as mr_runs

    _stub_ask_workbook(monkeypatch)
    seen = _seen_official(monkeypatch)

    def _boom(*a, **kw):
        raise mr_runs.RunStoreError("firestore unavailable")

    monkeypatch.setattr(mr_runs, "list_runs", _boom)
    r = client.post("/api/mr/ask", json={"question": "how much did we spend in August 2026"})
    assert r.status_code == 200, r.text
    assert seen == [{}]
    body = r.json()
    assert body["facts"] and body["ai"] is False and body["fallback_reason"]
    assert any("official totals unavailable" in f["basis"] for f in body["facts"])


# --- /mr/ask: second adversarial pass (2026-09-22) --------------------------------
# Workspace selection through `_ws`, bad input, and each failure mode of the
# three things the handler depends on (sheet read, official-totals read, model).
# Tests tagged "DEFECT2 <id>" are xfail(strict=True) and name the rule that would
# fix them; ids continue the list in test_workbook_intelligence.py.

def test_ask_reads_the_shared_workspaces_official_totals_when_sharing_is_on(monkeypatch):
    """`_ws(user)` is the only thing that picks the key. Sharing ON: the run under
    the deployment's workspace key is the one read - once, by kind - and the
    caller's own (different) official run is never consulted."""
    from marketing_research_agent import runs as mr_runs

    _stub_ask_workbook(monkeypatch)
    seen = _seen_official(monkeypatch)
    key = _shared_ws(monkeypatch)
    assert key != USER["id"]
    _save_official(key, {"2026-08": {"spend": 6500.0, "leads": 60}})
    _save_official(USER["id"], {"2026-08": {"spend": 999999.0}})
    calls: list[tuple] = []
    real_list = mr_runs.list_runs
    monkeypatch.setattr(mr_runs, "list_runs",
                        lambda uid, **kw: (calls.append((uid, kw.get("kind"))), real_list(uid, **kw))[1])
    monkeypatch.setattr(mr_router, "_load_dataset",
                        lambda *a, **kw: pytest.fail("/mr/ask loaded the whole dataset"))

    r = client.post("/api/mr/ask", json={"question": "how much did we spend in August 2026"})
    assert r.status_code == 200, r.text
    assert seen == [{"2026-08": {"spend": 6500.0, "leads": 60}}]
    assert calls == [(key, "official_spend")], calls
    assert any("the sheet's own Overall roll-up" in f["basis"] for f in r.json()["facts"])


def test_ask_uses_only_the_callers_own_official_run_when_sharing_is_off(monkeypatch):
    _stub_ask_workbook(monkeypatch)
    seen = _seen_official(monkeypatch)
    _save_official("a-colleague", {"2026-08": {"spend": 999999.0}})
    r = client.post("/api/mr/ask", json={"question": "how much did we spend in August 2026"})
    assert r.status_code == 200, r.text
    assert seen == [{}], "another workspace's roll-up must never reach the engine"
    assert all("official totals unavailable" in f["basis"] for f in r.json()["facts"]
               if f["label"].startswith("all channels — spend"))


@pytest.mark.parametrize("body", [None, {}, {"question": ""}, {"question": " \n\t "}, {"other": 1}])
def test_ask_without_a_question_is_a_400_and_never_reaches_the_engine(monkeypatch, body):
    _stub_ask_workbook(monkeypatch)
    monkeypatch.setattr(mr_router.mr_insight, "answer",
                        lambda *a, **k: pytest.fail("an empty question reached the engine"))
    r = client.post("/api/mr/ask", json=body)
    assert r.status_code == 400 and r.json()["detail"] == "question is required"


@pytest.mark.parametrize("body", [[1], "spend", 5])
def test_ask_with_a_body_that_is_not_an_object_is_a_422_not_a_500(monkeypatch, body):
    _stub_ask_workbook(monkeypatch)
    r = client.post("/api/mr/ask", json=body)
    assert r.status_code == 422, r.text


@pytest.mark.parametrize("question", [None, 5, ["spend"], {"q": 1}])
def test_defect2_r2_a_non_string_question_is_a_400(monkeypatch, question):
    _stub_ask_workbook(monkeypatch)
    r = client.post("/api/mr/ask", json={"question": question})
    assert r.status_code == 400, (r.status_code, r.text[:200])


def test_defect2_r5_an_enormous_question_is_refused_before_it_reaches_a_paid_model(monkeypatch):
    _stub_ask_workbook(monkeypatch)
    r = client.post("/api/mr/ask", json={"question": "spend in August " * 20_000})
    assert r.status_code in (400, 413, 422), r.status_code


@pytest.mark.parametrize("timeframe", [5, 0, True, {"a": 1}, [], "", "monthly", "2026-13", "9999-12", "0000-01"])
def test_ask_with_a_junk_timeframe_is_an_honest_refusal_never_a_500(monkeypatch, timeframe):
    _stub_ask_workbook(monkeypatch)
    r = client.post("/api/mr/ask", json={"question": "how much did we spend", "timeframe": timeframe})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["period_label"] is None and body["ai"] is False and body["fallback_reason"]
    assert body["facts"] == []


def test_ask_when_the_model_call_raises_still_answers_with_the_exact_figures(monkeypatch):
    from app.services import openrouter

    def _boom(**kw):
        raise RuntimeError("upstream exploded")

    _stub_ask_workbook(monkeypatch)
    monkeypatch.setattr(mr_router, "_latest_official_run", lambda uid, all_runs=None: {})
    monkeypatch.delenv("MR_OFFLINE", raising=False)      # take the real model branch
    monkeypatch.setattr(openrouter, "get_llm", _boom)
    r = client.post("/api/mr/ask", json={"question": "how much did we spend in August 2026"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ai"] is False and "call failed" in body["fallback_reason"]
    assert body["facts"] and "$4,000.00" in body["answer"]
    assert "Traceback" not in r.text


def test_ask_when_the_sheet_cannot_be_read_is_a_502_with_a_message_not_a_trace(monkeypatch):
    def _boom(**kw):
        raise RuntimeError("sheets backend error 503")

    monkeypatch.setattr(mr_router, "_workbook_bundle", _boom)
    r = client.post("/api/mr/ask", json={"question": "spend in August 2026"})
    assert r.status_code == 502
    detail = r.json()["detail"]
    assert detail.startswith("Could not read the spreadsheet") and "Traceback" not in detail


def test_ask_when_the_official_read_fails_costs_only_the_parity_note(monkeypatch):
    """The read raising must neither 502 nor 500 Ask, and the sheet is still read
    exactly once."""
    from marketing_research_agent import runs as mr_runs

    _stub_ask_workbook(monkeypatch)
    reads: list[int] = []
    real_bundle = mr_router._workbook_bundle
    monkeypatch.setattr(mr_router, "_workbook_bundle",
                        lambda **kw: (reads.append(1), real_bundle(**kw))[1])

    def _boom(*a, **kw):
        raise mr_runs.RunStoreError("firestore unavailable")

    monkeypatch.setattr(mr_runs, "list_runs", _boom)
    r = client.post("/api/mr/ask", json={"question": "how much did we spend in August 2026"})
    assert r.status_code == 200, r.text
    assert reads == [1]
    assert "firestore unavailable" not in r.text, "the store's message must not reach the caller"
