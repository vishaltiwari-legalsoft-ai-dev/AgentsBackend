"""Integration tests for the Graphics Designer Element Library API (/api/gd).

Runs fully offline: the ``fs`` run-storage backend is used (default, pointed at
a tmp dir via ``GD_RUNS_DIR``) and the auth dependency is overridden with a fake
user, mirroring ``test_mr_router.py``'s pattern for the MR router.
"""

import io

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.security import get_current_user

app.dependency_overrides[get_current_user] = lambda: {"id": "u1", "email": "t@legalsoft.com"}
client = TestClient(app)


@pytest.fixture(autouse=True)
def _runs_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("GD_RUNS_DIR", str(tmp_path))


@pytest.fixture()
def a_run_id():
    r = client.post("/api/gd/runs", json={})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def test_gd_elements_catalog():
    r = client.get("/api/gd/elements")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "emoji" in body and "icons" in body and "stickers" in body
    assert isinstance(body["emoji"], list)
    assert isinstance(body["icons"], list)
    assert isinstance(body["stickers"], list)
    assert "max_elements" in body


def test_config_accepts_elements(a_run_id):
    r = client.post(
        f"/api/gd/runs/{a_run_id}/config",
        json={"elements": [{"kind": "emoji", "ref": "\U0001F600", "x": 0.5, "y": 0.5}]},
    )
    assert r.status_code == 200, r.text
    assert r.json()["config"]["elements"][0]["kind"] == "emoji"


def test_config_rejects_bad_element_kind(a_run_id):
    r = client.post(
        f"/api/gd/runs/{a_run_id}/config",
        json={"elements": [{"kind": "nope", "ref": "x"}]},
    )
    assert r.status_code == 400


def test_element_upload_png(a_run_id):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (4, 4), (255, 0, 0, 255)).save(buf, format="PNG")
    buf.seek(0)
    r = client.post(
        f"/api/gd/runs/{a_run_id}/elements/upload",
        files={"file": ("sprite.png", buf, "image/png")},
    )
    assert r.status_code == 200, r.text
    assert "ref" in r.json() and r.json()["ref"]


def test_element_upload_rejects_bad_content_type(a_run_id):
    r = client.post(
        f"/api/gd/runs/{a_run_id}/elements/upload",
        files={"file": ("sprite.txt", io.BytesIO(b"not an image"), "text/plain")},
    )
    assert r.status_code == 400
