"""Storage seam — fs is the default and round-trips; cloud mode routes run
manifests to Firestore and artifacts to GCS. Cloud is exercised with in-memory
fakes so the test stays offline (no GCP)."""

import sys
import types

import pytest

from graphics_designer_agent import runs


def test_default_backend_is_fs():
    assert runs.GD_STORAGE_BACKEND == "fs"
    assert runs._use_cloud() is False


def test_fs_run_and_artifact_roundtrip():
    run = runs.create_run("u", "legalsoft")
    got = runs.get_run(run["id"])
    assert got and got["id"] == run["id"]
    rel = runs.save_artifact(run["id"], 1, "A", 1, b"PNGDATA")
    assert not rel.startswith("gs://")  # fs ref is a run-relative path
    assert runs.read_artifact(run["id"], rel) == b"PNGDATA"


def _install_cloud_fakes(monkeypatch):
    """Inject minimal in-memory app.services.{firestore_repo,storage}."""
    cols: dict = {}

    class _Doc:
        def __init__(self, store, did):
            self._s, self._id = store, did

        def set(self, data):
            self._s[self._id] = dict(data)

        def get(self):
            d = self._s.get(self._id)
            return types.SimpleNamespace(exists=self._id in self._s, to_dict=lambda: d)

    class _Col:
        def __init__(self, store):
            self._s = store

        def document(self, did):
            return _Doc(self._s, did)

    class _DB:
        def collection(self, name):
            return _Col(cols.setdefault(name, {}))

    fr = types.ModuleType("app.services.firestore_repo")
    fr._db = lambda: _DB()

    blobs: dict = {}
    st = types.ModuleType("app.services.storage")

    def _upload(partition, file_name, data, content_type):
        uri = f"gs://bucket/generated/{partition}/{file_name}"
        blobs[uri] = data
        return uri, uri + "?sig"

    st.upload_generated = _upload
    st.download_bytes = lambda uri: blobs[uri]

    services = types.ModuleType("app.services")
    services.storage = st
    services.firestore_repo = fr
    app = types.ModuleType("app")
    app.services = services
    for name, mod in {
        "app": app, "app.services": services,
        "app.services.storage": st, "app.services.firestore_repo": fr,
    }.items():
        monkeypatch.setitem(sys.modules, name, mod)
    monkeypatch.setattr(runs, "GD_STORAGE_BACKEND", "cloud")
    # ``is_own_artifact_ref``'s bucket-pin check reads the REAL app.config.settings
    # (not faked above) whenever the backend app is importable in-process; pin it
    # to match the fake uploader's "bucket" so the ref round-trip below isn't
    # rejected as a foreign bucket.
    try:
        from app.config import settings as _real_settings

        monkeypatch.setattr(_real_settings, "gcs_bucket_name", "bucket", raising=False)
    except Exception:  # noqa: BLE001 - backend app not importable here; nothing to pin
        pass
    return cols, blobs


def test_cloud_routes_runs_to_firestore_and_artifacts_to_gcs(monkeypatch):
    cols, blobs = _install_cloud_fakes(monkeypatch)
    assert runs._use_cloud() is True

    run = runs.create_run("u", "legalsoft")
    assert run["id"] in cols["gd_runs"]                 # manifest in Firestore
    assert runs.get_run(run["id"])["id"] == run["id"]   # read back from Firestore

    ref = runs.save_artifact(run["id"], 2, "B", 1, b"CLOUDPNG")
    assert ref.startswith("gs://") and blobs[ref] == b"CLOUDPNG"   # artifact in GCS
    assert runs.read_artifact(run["id"], ref) == b"CLOUDPNG"       # gs:// read routes to GCS


# ── C1 regression: cross-run / arbitrary GCS object read ──────────────────────
# An ``image`` element's ``ref`` used to reach ``storage.download_bytes`` with NO
# ownership check, so an authenticated user could point it at another run's (or
# any SA-readable) ``gs://`` object and have the server fetch it with its own
# credentials. ``read_artifact`` must now refuse anything outside this run's own
# ``generated/gd/<run_id>/`` partition.
def test_read_artifact_rejects_foreign_run_gs_ref(monkeypatch):
    _install_cloud_fakes(monkeypatch)
    victim = runs.create_run("victim", "legalsoft")
    attacker = runs.create_run("attacker", "legalsoft")
    victim_ref = runs.save_artifact(victim["id"], 3, "upload", 1, b"SECRET")

    with pytest.raises(ValueError):
        runs.read_artifact(attacker["id"], victim_ref)


def test_read_artifact_rejects_arbitrary_gs_uri(monkeypatch):
    _install_cloud_fakes(monkeypatch)
    run = runs.create_run("u", "legalsoft")
    with pytest.raises(ValueError):
        runs.read_artifact(run["id"], "gs://some-other-bucket/totally/unrelated/object.png")


def test_read_artifact_allows_own_run_gs_ref(monkeypatch):
    _install_cloud_fakes(monkeypatch)
    run = runs.create_run("u", "legalsoft")
    ref = runs.save_artifact(run["id"], 3, "upload", 1, b"MINE")
    assert runs.read_artifact(run["id"], ref) == b"MINE"  # legitimate path unaffected


def test_is_own_artifact_ref_fs_mode_accepts_relative_paths():
    # fs mode never carries a run_id-partitioned ref format; containment is left
    # to artifact_abspath's traversal guard, so any non-empty relative ref passes
    # this earlier ownership gate.
    assert runs.is_own_artifact_ref("run123", "stage-3/upload-abc.png") is True
    assert runs.is_own_artifact_ref("run123", "") is False
