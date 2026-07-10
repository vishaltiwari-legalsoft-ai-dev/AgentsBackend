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
