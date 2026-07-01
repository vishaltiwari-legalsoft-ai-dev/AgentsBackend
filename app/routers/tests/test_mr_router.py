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
