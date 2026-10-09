"""Run persistence (spec §3 "State", §8 export).

Each run is a directory under ``GD_RUNS_DIR`` (default: ``<agent>/runs``)
containing ``run.json`` (the full manifest) plus every generated artifact under
``stage-<n>/<variant>-<attempt>.png``. Nothing is ever deleted — the review
screen can re-approve any past attempt (§8).
"""

from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .stage4_logo.compositor import default_logo_layout
from .tokens import DEFAULT_AR, DEFAULT_CTA_PLACEMENT, DEFAULT_TEXT_PLACEMENT

# Per-brand factory defaults (font, copy, element styles, sub-headings) come from
# the resolved BrandPack inside ``create_run`` — see ``registry.get_pack``.

RUNS_ROOT = Path(os.environ.get("GD_RUNS_DIR") or (Path(__file__).resolve().parents[1] / "runs"))

# Storage backend (scalability seam, see runs §). ``fs`` keeps every run manifest
# and artifact on the local filesystem — correct for tests and single-machine dev,
# but per-instance and ephemeral on Cloud Run. ``cloud`` persists manifests to the
# ``gd_runs`` Firestore collection and artifacts to GCS (via ``app.services``), so
# state is shared across instances and survives redeploys. Default is ``fs`` so the
# offline test suite and the standalone package are unchanged; a multi-instance
# deployment must set ``GD_STORAGE_BACKEND=cloud`` (on ``fs`` every run lives on
# the instance that created it and is lost on scale-down or deploy). Read at
# import, so flipping it takes a new revision. App-service imports stay lazy
# (inside the cloud branch) so the package still imports without the backend app.
GD_STORAGE_BACKEND = (os.environ.get("GD_STORAGE_BACKEND") or "fs").strip().lower()

# GCS object prefix for this agent's artifacts: ``generated/gd/<run_id>/...``.
_GCS_PARTITION = "gd"

# A cloud-mode artifact ref is the object's flat file name inside its run's
# partition (``stage-2-A-1.png``) — never a ``gs://`` URI and never a path. That
# is what lets the browser fetch it through the API proxy (a ``gs://`` ref did
# not survive the Vercel relay's path rejoin: ``//`` collapses), and what makes
# a client-supplied ref unable to name anything outside its own run: there is
# no bucket and no "/" in it to point elsewhere.
_ARTIFACT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_UNSAFE_NAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")

STATE_FOR_STAGE_CONFIG = {1: "STAGE1_CONFIG", 2: "STAGE2_CONFIG", 3: "STAGE3_CONFIG", 4: "STAGE4_CONFIG"}
STATE_FOR_STAGE_REVIEW = {1: "STAGE1_REVIEW", 2: "STAGE2_REVIEW", 3: "STAGE3_REVIEW", 4: "STAGE4_REVIEW"}


def _use_cloud() -> bool:
    return GD_STORAGE_BACKEND == "cloud"


def uses_cloud_storage() -> bool:
    """Whether run manifests + artifacts live in Firestore/GCS (shared by every
    instance) rather than on this instance's disk."""
    return _use_cloud()


def _gd_runs_collection():
    """The ``gd_runs`` Firestore collection (lazy — only imported in cloud mode)."""
    from app.services.firestore_repo import _db

    return _db().collection("gd_runs")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_stage() -> dict:
    return {"variant": None, "attempts": [], "approved": None}


def create_run(user_id: str, brand_id: str | None = None) -> dict:
    # Resolve the selected brand's pack so every factory default (font, copy,
    # element styles, sub-headings) starts from that brand's identity. None means
    # Legal Soft; an unknown id raises ``registry.UnknownBrand`` (the router
    # answers 404) — a run must never silently start as a different brand.
    # Imported lazily to avoid an import cycle (registry imports the content
    # modules that ultimately import runs' siblings).
    from . import registry

    pack = registry.get_pack(brand_id)
    run = {
        "id": uuid.uuid4().hex[:12],
        "user_id": user_id,
        "brand_id": pack.id,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "state": "STAGE1_CONFIG",
        "config": {
            "font": pack.default_font,
            "aspect_ratio": DEFAULT_AR,
            "text_placement": DEFAULT_TEXT_PLACEMENT,
            "cta_placement": DEFAULT_CTA_PLACEMENT,
            # Per-element Stage-3 styling for the deterministic renderer: headline +
            # CTA carry font/colour/size/placement/pixel-nudge; highlight is inline
            # (font + colour). Sub-headings are the dynamic list below.
            "element_styles": pack.default_stage3_styles(),
            # Stage-3 sub-heading lines (1–5). Each carries its own text + styling.
            "subheadings": pack.default_subheadings(),
            # Stage-4 logo placement controls (deterministic compositor).
            "logo_layout": default_logo_layout(),
            "use_ai_compositor": False,
            # Per-creative AI gradient (Stage 1). Temporary + non-canonical: an
            # agent-proposed gradient stored ONLY here, never written to prompts/
            # or added to CANONICAL_SHA256 / STAGE1_VARIANTS. None until proposed.
            "custom_gradient": None,
            # Per-creative AI element (Stage 2). Temporary + non-canonical: an
            # agent-proposed foreground subject stored ONLY here, never added to
            # STAGE2_VARIANTS. None until proposed. Selected with variant "AI".
            "custom_element": None,
            # Pre-generation discovery brief (the "micro-conversation" answers:
            # feeling/audience/tone/style/event/theme). Folded into every suggestion
            # so the agent gathers intent BEFORE proposing. Empty until answered.
            "creative_brief": {},
            # Headline/highlight/CTA text. Sub-heading text lives in ``subheadings``.
            "tokens": {
                "headline": pack.default_headline,
                "highlight": pack.default_highlight,
                "cta": pack.default_cta,
                # Optional Stage-3 detail fields — empty until the user fills them;
                # become draggable text layers only when non-empty.
                "venue": "",
                "website": "",
            },
            "tokens_approved": {"headline": False, "highlight": False, "cta": False},
        },
        "stages": {str(n): _empty_stage() for n in range(1, 5)},
        "logo": None,
        "manifest_log": [],
    }
    save_run(run)
    return run


def run_dir(run_id: str) -> Path:
    return RUNS_ROOT / run_id


def run_json_path(run_id: str) -> Path:
    return run_dir(run_id) / "run.json"


def save_run(run: dict) -> None:
    run["updated_at"] = now_iso()
    if _use_cloud():
        # Last-write-wins, matching the existing filesystem semantics. Concurrent
        # writes to one run are still racy here exactly as they were on the FS; the
        # transactional attempt-append is the documented next increment and does not
        # change this storage seam.
        _gd_runs_collection().document(run["id"]).set(run)
        return
    d = run_dir(run["id"])
    d.mkdir(parents=True, exist_ok=True)
    run_json_path(run["id"]).write_text(json.dumps(run, indent=2, ensure_ascii=False), encoding="utf-8")


def get_run(run_id: str) -> dict | None:
    if _use_cloud():
        doc = _gd_runs_collection().document(run_id).get()
        return doc.to_dict() if doc.exists else None
    path = run_json_path(run_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _artifact_object_name(stage: int, variant: str, attempt) -> str:
    """The flat object name a cloud-mode artifact is stored (and referenced) by.
    Anything outside ``[A-Za-z0-9._-]`` is folded to ``_`` (and ``..`` to ``.``)
    so the name is always a valid ref (see ``_ARTIFACT_NAME_RE``) and never needs
    URL escaping."""
    name = _UNSAFE_NAME_CHARS.sub("_", f"stage-{stage}-{variant}-{attempt}.png")
    return re.sub(r"\.{2,}", ".", name)


def _is_artifact_name(ref: str) -> bool:
    return bool(_ARTIFACT_NAME_RE.match(ref)) and ".." not in ref


def save_artifact(run_id: str, stage: int, variant: str, attempt: int, png: bytes) -> str:
    """Persist a generated PNG and return an opaque reference to it.

    The reference is stored verbatim on the attempt and round-tripped through
    ``read_artifact`` / the router. In ``fs`` mode it is the run-relative path
    (``stage-<n>/<variant>-<attempt>.png``); in ``cloud`` mode it is the flat
    object name inside the run's GCS partition (``stage-<n>-<variant>-<attempt>.png``
    under ``generated/gd/<run_id>/``), readable from any instance.
    """
    if _use_cloud():
        from app.services import storage

        name = _artifact_object_name(stage, variant, attempt)
        storage.put_generated(
            partition=f"{_GCS_PARTITION}/{run_id}",
            file_name=name,
            data=png,
            content_type="image/png",
        )
        return name
    rel = f"stage-{stage}/{variant}-{attempt}.png"
    abspath = run_dir(run_id) / rel
    abspath.parent.mkdir(parents=True, exist_ok=True)
    abspath.write_bytes(png)
    return rel


def save_upload_artifact(run_id: str, name: str, data: bytes, content_type: str) -> str:
    """Persist a user upload's WORKING copy under a caller-chosen flat name
    (``<md5>-w4096.png`` / ``.jpg``) and return its ref.

    ``save_artifact``'s sibling for direct uploads, whose names are
    content-addressed by the original's GCS MD5 (so a repeated finalize lands
    on the same ref) and whose extension says the real format — a background's
    working copy is a JPEG. The name must already be a valid flat artifact
    name; it is resolved only inside this run's own space, exactly like every
    other ref (``is_own_artifact_ref``)."""
    if not _is_artifact_name(name):
        raise ValueError(f"invalid upload artifact name {name!r}")
    if _use_cloud():
        from app.services import storage

        storage.put_generated(partition=f"{_GCS_PARTITION}/{run_id}", file_name=name,
                              data=data, content_type=content_type)
        return name
    abspath = artifact_abspath(run_id, name)
    abspath.parent.mkdir(parents=True, exist_ok=True)
    abspath.write_bytes(data)
    return name


def originals_prefix(run_id: str) -> str:
    """GCS object prefix (no bucket) for a run's uploaded ORIGINALS:
    ``generated/gd/<run_id>/originals/``. Nothing in the pipeline reads it and
    no artifact ref can name it (a ref carries no ``/``)."""
    return f"{_gcs_partition_prefix(run_id)}originals/"


def _gcs_partition_prefix(run_id: str) -> str:
    """The GCS object prefix (no bucket) that ``save_artifact`` writes this run's
    artifacts under: ``generated/gd/<run_id>/``. Any legitimate ``gs://`` ref for
    this run MUST live under this prefix — see ``put_generated`` in
    ``app.services.storage`` (``generated/<partition>/<file_name>``)."""
    return f"generated/{_GCS_PARTITION}/{run_id}/"


def _own_gs_object_name(run_id: str, ref: str) -> str | None:
    """For a legacy ``gs://`` ref (stored before refs became bare object
    names): the flat object name when the URI sits directly inside this run's
    own partition, else None."""
    rest = ref[len("gs://"):]
    bucket, _, object_path = rest.partition("/")
    if not bucket or not object_path:
        return None
    prefix = _gcs_partition_prefix(run_id)
    if not object_path.startswith(prefix):
        return None
    name = object_path[len(prefix):]
    return name if _is_artifact_name(name) else None


def is_own_artifact_ref(run_id: str, ref: str) -> bool:
    """Whether ``ref`` points inside ``run_id``'s own artifact space.

    fs mode: any relative path — real containment is enforced by
    ``artifact_abspath``'s path-traversal guard, so a bare non-empty relative
    ref is accepted here (the traversal guard runs at read time).

    cloud mode: the ref is a flat object name (``[A-Za-z0-9._-]``, no ``/``,
    no ``..``) that is only ever resolved inside this run's own
    ``generated/gd/<run_id>/`` partition, so it cannot name another run's — or
    any other — object. Legacy ``gs://`` refs must sit under that same
    partition. This is the fix for the cross-run / arbitrary GCS-read
    vulnerability (C1): without it, any authenticated user could set an
    ``image`` element's ``ref`` to another run's (or any SA-readable) object
    and have the server fetch it with its own service-account credentials.

    For ``gs://`` refs the bucket is additionally pinned to the configured
    ``gcs_bucket_name`` when that setting is available, as defense in depth
    against a ref pointing at a *different* bucket; the partition-prefix
    check is the primary (and only strictly required) gate, so this stays safe
    to run even where settings aren't wired up (e.g. tests that fake only
    ``app.services.storage``).
    """
    if not isinstance(ref, str) or not ref.strip():
        return False
    if not ref.startswith("gs://"):
        if _use_cloud():
            return _is_artifact_name(ref)
        # fs-mode relative ref — containment checked by artifact_abspath at read time.
        return True
    if _own_gs_object_name(run_id, ref) is None:
        return False
    bucket = ref[len("gs://"):].partition("/")[0]
    try:
        from app.config import settings

        expected_bucket = getattr(settings, "gcs_bucket_name", "") or ""
    except Exception:  # noqa: BLE001 - settings unavailable outside the backend app
        expected_bucket = ""
    if expected_bucket and bucket != expected_bucket:
        return False
    return True


def artifact_url_ref(run_id: str, ref: str) -> str:
    """The path segment the API proxy serves ``ref`` under
    (``/api/gd/runs/<run_id>/artifact/<this>``).

    Cloud names and fs relative paths are returned as-is; a legacy ``gs://``
    ref of this run is reduced to its object name, so every URL the client
    receives is a plain relative path that survives the frontend's
    ``${API_URL}${path}`` join and the relay's per-segment re-encoding."""
    if isinstance(ref, str) and ref.startswith("gs://"):
        return _own_gs_object_name(run_id, ref) or ref
    return ref


def read_artifact(run_id: str, ref: str) -> bytes:
    """Read an artifact's bytes from its stored reference (fs path, cloud
    object name, or a legacy gs:// URI).

    Enforces that ``ref`` belongs to ``run_id``'s own artifact space — see
    ``is_own_artifact_ref`` — so a foreign/cross-run ref can never be fetched
    with the server's service-account credentials (C1 fix). Every legitimate
    caller already only ever reads its own run's artifacts, so this check is
    safe to apply unconditionally.

    Cloud mode reads from GCS, never this instance's disk, so a run created on
    another instance (or before the last deploy) resolves the same here. A
    missing object raises :class:`FileNotFoundError`.
    """
    if not is_own_artifact_ref(run_id, ref):
        raise ValueError(f"artifact ref does not belong to run {run_id!r}")
    if ref.startswith("gs://"):
        from app.services import storage

        return storage.download_bytes(ref)
    if _use_cloud():
        from app.services import storage

        return storage.read_generated(f"{_GCS_PARTITION}/{run_id}", ref)
    return artifact_abspath(run_id, ref).read_bytes()


def artifact_abspath(run_id: str, rel: str) -> Path:
    # Guard against path traversal (filesystem backend only).
    base = run_dir(run_id).resolve()
    target = (run_dir(run_id) / rel).resolve()
    if not str(target).startswith(str(base)):
        raise ValueError("invalid artifact path")
    return target


def log_manifest(run: dict, token: str, source: str, original_suggestion, final_value) -> None:
    """Audit log entry for every value that reaches a prompt (spec §7.2)."""
    run["manifest_log"].append(
        {
            "token": token,
            "source": source,  # "user" | "agent"
            "original_suggestion": original_suggestion,
            "final_value": final_value,
            "timestamp": now_iso(),
        }
    )
