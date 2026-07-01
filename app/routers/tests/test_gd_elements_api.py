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


def _png_bytes(color):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (4, 4), color).save(buf, format="PNG")
    buf.seek(0)
    return buf.read()


def test_element_upload_distinct_images_get_distinct_refs(a_run_id):
    """Regression: uploads used to be numbered by a never-populated
    ``run["uploads"]`` counter, so every upload computed attempt=1 and every
    image landed at the same artifact path — a second upload silently
    overwrote the first. Distinct image bytes must now produce distinct refs.
    """
    red = _png_bytes((255, 0, 0, 255))
    blue = _png_bytes((0, 0, 255, 255))

    r1 = client.post(
        f"/api/gd/runs/{a_run_id}/elements/upload",
        files={"file": ("red.png", io.BytesIO(red), "image/png")},
    )
    r2 = client.post(
        f"/api/gd/runs/{a_run_id}/elements/upload",
        files={"file": ("blue.png", io.BytesIO(blue), "image/png")},
    )
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    ref1, ref2 = r1.json()["ref"], r2.json()["ref"]
    assert ref1 and ref2
    assert ref1 != ref2


def test_element_upload_same_bytes_dedupe_to_same_ref(a_run_id):
    """Re-uploading identical bytes should resolve to the same content-hash
    artifact path rather than growing a new (numbered) file each time."""
    red = _png_bytes((255, 0, 0, 255))

    r1 = client.post(
        f"/api/gd/runs/{a_run_id}/elements/upload",
        files={"file": ("red.png", io.BytesIO(red), "image/png")},
    )
    r2 = client.post(
        f"/api/gd/runs/{a_run_id}/elements/upload",
        files={"file": ("red-again.png", io.BytesIO(red), "image/png")},
    )
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    assert r1.json()["ref"] == r2.json()["ref"]
