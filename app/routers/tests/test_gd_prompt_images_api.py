"""Integration tests for prompt-image attach (/subject/upload?role=prompt).

Offline: fs run-storage via GD_RUNS_DIR tmp dir; auth dependency overridden —
mirrors test_gd_elements_api.py.

The auth override is installed per-test by the ``_harness`` fixture and the
previous value is restored on teardown. It must never be applied at module
import time: ``dependency_overrides`` lives on the one process-global FastAPI
app shared by every test module, so an import-time write leaks into whatever
runs next and a sibling module's teardown silently deletes it — which made this
suite pass only in alphabetical order (a 401 storm under ``-k``, ``-m``,
sharding or random ordering).
"""

import io

import pytest
from fastapi.testclient import TestClient

from app.main import app as fastapi_app
from app.security import get_current_user

client = TestClient(fastapi_app)

USER = {"id": "u1", "email": "t@legalsoft.com"}


@pytest.fixture(autouse=True)
def _harness(tmp_path, monkeypatch):
    monkeypatch.setenv("GD_RUNS_DIR", str(tmp_path))
    prev = fastapi_app.dependency_overrides.get(get_current_user)
    fastapi_app.dependency_overrides[get_current_user] = lambda: dict(USER)
    yield
    if prev is None:
        fastapi_app.dependency_overrides.pop(get_current_user, None)
    else:
        fastapi_app.dependency_overrides[get_current_user] = prev


@pytest.fixture()
def a_run_id():
    r = client.post("/api/gd/runs", json={})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _png_bytes(color):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGBA", (4, 4), color).save(buf, format="PNG")
    buf.seek(0)
    return buf.read()


def _upload(run_id, data, name="ref.png"):
    return client.post(
        f"/api/gd/runs/{run_id}/subject/upload?role=prompt",
        files={"file": (name, io.BytesIO(data), "image/png")},
    )


def test_prompt_upload_appends_to_config(a_run_id):
    r = _upload(a_run_id, _png_bytes((255, 0, 0, 255)))
    assert r.status_code == 200, r.text
    assert r.json()["role"] == "prompt"
    run = client.get(f"/api/gd/runs/{a_run_id}").json()
    assert run["config"]["prompt_image_refs"] == [r.json()["ref"]]


def test_prompt_upload_caps_at_three(a_run_id):
    for c in [(255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255)]:
        assert _upload(a_run_id, _png_bytes(c)).status_code == 200
    r4 = _upload(a_run_id, _png_bytes((9, 9, 9, 255)))
    assert r4.status_code == 400
    run = client.get(f"/api/gd/runs/{a_run_id}").json()
    assert len(run["config"]["prompt_image_refs"]) == 3


def test_prompt_upload_dedupes_identical_bytes(a_run_id):
    red = _png_bytes((255, 0, 0, 255))
    r1, r2 = _upload(a_run_id, red), _upload(a_run_id, red, "red-again.png")
    assert r1.status_code == 200 and r2.status_code == 200
    assert r1.json()["ref"] == r2.json()["ref"]
    run = client.get(f"/api/gd/runs/{a_run_id}").json()
    assert run["config"]["prompt_image_refs"] == [r1.json()["ref"]]  # one entry, no dupe


def test_bad_role_still_rejected(a_run_id):
    r = client.post(
        f"/api/gd/runs/{a_run_id}/subject/upload?role=banner",
        files={"file": ("s.png", io.BytesIO(_png_bytes((1, 2, 3, 255))), "image/png")},
    )
    assert r.status_code == 400


def test_subject_role_untouched_by_this_change(a_run_id):
    r = client.post(
        f"/api/gd/runs/{a_run_id}/subject/upload?role=subject",
        files={"file": ("s.png", io.BytesIO(_png_bytes((1, 2, 3, 255))), "image/png")},
    )
    assert r.status_code == 200, r.text
    run = client.get(f"/api/gd/runs/{a_run_id}").json()
    assert run["config"].get("prompt_image_refs") in (None, [])
