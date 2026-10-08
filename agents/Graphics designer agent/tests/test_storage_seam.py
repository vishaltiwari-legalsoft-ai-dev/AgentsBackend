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

    blobs: dict = {}   # object path (no bucket) -> bytes
    st = types.ModuleType("app.services.storage")

    def _put(partition, file_name, data, content_type):
        blobs[f"generated/{partition}/{file_name}"] = data
        return f"gs://bucket/generated/{partition}/{file_name}"

    def _read(partition, file_name):
        try:
            return blobs[f"generated/{partition}/{file_name}"]
        except KeyError:
            raise FileNotFoundError(file_name) from None

    def _download(uri):
        return blobs[uri.split("/", 3)[3]]

    st.put_generated = _put
    st.read_generated = _read
    st.download_bytes = _download

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
    # The ref is the flat object name inside the run's partition — no bucket, no
    # "/", nothing the Vercel relay's path rejoin can mangle.
    assert ref == "stage-2-B-1.png"
    assert blobs[f"generated/gd/{run['id']}/stage-2-B-1.png"] == b"CLOUDPNG"  # artifact in GCS
    assert runs.read_artifact(run["id"], ref) == b"CLOUDPNG"                 # read routes to GCS
    assert runs.artifact_url_ref(run["id"], ref) == ref


def test_cloud_names_are_always_valid_refs(monkeypatch):
    """Whatever a variant id carries, the stored name is a ref the ownership
    gate accepts and a URL segment that needs no escaping."""
    _install_cloud_fakes(monkeypatch)
    run = runs.create_run("u", "legalsoft")
    ref = runs.save_artifact(run["id"], 3, "weird/../id é", "ab12", b"X")
    assert "/" not in ref and ".." not in ref
    assert runs.is_own_artifact_ref(run["id"], ref)
    assert runs.read_artifact(run["id"], ref) == b"X"


def test_legacy_gs_ref_still_reads_and_serves_by_name(monkeypatch):
    """Docs written before refs became names carry full gs:// URIs: they still
    read, and the URL handed to the browser is the plain object name."""
    _, blobs = _install_cloud_fakes(monkeypatch)
    run = runs.create_run("u", "legalsoft")
    legacy = f"gs://bucket/generated/gd/{run['id']}/stage-1-A-1.png"
    blobs[legacy.split("/", 3)[3]] = b"OLD"
    assert runs.read_artifact(run["id"], legacy) == b"OLD"
    assert runs.artifact_url_ref(run["id"], legacy) == "stage-1-A-1.png"


def test_missing_cloud_artifact_is_file_not_found(monkeypatch):
    _install_cloud_fakes(monkeypatch)
    run = runs.create_run("u", "legalsoft")
    with pytest.raises(FileNotFoundError):
        runs.read_artifact(run["id"], "stage-9-Z-1.png")


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
    runs.save_artifact(victim["id"], 3, "upload", 1, b"SECRET")
    victim_uri = f"gs://bucket/generated/gd/{victim['id']}/stage-3-upload-1.png"

    with pytest.raises(ValueError):
        runs.read_artifact(attacker["id"], victim_uri)


def test_a_bare_name_only_ever_resolves_inside_the_callers_own_run(monkeypatch):
    """The victim's object NAME, replayed on the attacker's run, is looked up in
    the attacker's partition — it can never return the victim's bytes."""
    _install_cloud_fakes(monkeypatch)
    victim = runs.create_run("victim", "legalsoft")
    attacker = runs.create_run("attacker", "legalsoft")
    victim_ref = runs.save_artifact(victim["id"], 3, "upload", 1, b"SECRET")

    with pytest.raises(FileNotFoundError):
        runs.read_artifact(attacker["id"], victim_ref)
    for escape in (f"../{victim['id']}/{victim_ref}", f"{victim['id']}/{victim_ref}",
                   "..", ".hidden", "a\b.png"):
        assert runs.is_own_artifact_ref(attacker["id"], escape) is False
        with pytest.raises(ValueError):
            runs.read_artifact(attacker["id"], escape)


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
