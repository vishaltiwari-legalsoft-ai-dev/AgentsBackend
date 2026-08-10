"""Integration tests for the Marketing Research router (/api/mr).

Runs fully offline: MR_OFFLINE=1 forces the deterministic narrative path and
disables cloud writes; the auth dependency is overridden with a fake user.
"""

import io
import os

os.environ["MR_OFFLINE"] = "1"

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.security import get_current_user

app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "email": "t@legalsoft.com"}
client = TestClient(app)

CSV = (
    b"Campaign,Cost,Source,Medium,Campaign name,Leads,Qualified leads,"
    b"Demos booked,Demos completed,Day\n"
    b"PI,1200,google,cpc,pi,12,9,4,2,2026-06-29\n"
)


@pytest.fixture(autouse=True)
def _runs_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("MR_RUNS_DIR", str(tmp_path))
    monkeypatch.setenv("MR_TARGETS_FILE", str(tmp_path / "targets.json"))


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
    monkeypatch.setattr(mrr, "fetch_official_totals", lambda sid, year: {})
    monkeypatch.setattr(mrr.mr_workbook, "fetch_workbook", lambda sid, **kw: [tab])
    monkeypatch.setattr(mrr, "fetch_tab_values", lambda sid, title: rows)

    r = client.post("/api/mr/ingest-sheet", json={})
    assert r.status_code == 200, r.text
    assert any(str(t.get("tab", "")).startswith("Lead analysis") for t in r.json()["tabs"])

    body = client.get("/api/mr/lead-analysis").json()
    assert body["has_data"] is True and body["tab"] == "Lead Analysis"
    v = body["months"]["2026-08"]["vendors"][0]
    assert v["booked"] == 2 and v["completed"] == 1 and v["no_show"] == 1
    assert v["services_sold"] == 1 and v["amount"] == 2000.0
