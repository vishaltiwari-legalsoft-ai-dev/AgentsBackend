"""Run persistence (spec §3 "State", §8 export).

Each run is a directory under ``GD_RUNS_DIR`` (default: ``<agent>/runs``)
containing ``run.json`` (the full manifest) plus every generated artifact under
``stage-<n>/<variant>-<attempt>.png``. Nothing is ever deleted — the review
screen can re-approve any past attempt (§8).
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .compositor import default_logo_layout
from .tokens import DEFAULT_AR, DEFAULT_CTA_PLACEMENT, DEFAULT_TEXT_PLACEMENT

# Per-brand factory defaults (font, copy, element styles, sub-headings) come from
# the resolved BrandPack inside ``create_run`` — see ``registry.get_pack``.

RUNS_ROOT = Path(os.environ.get("GD_RUNS_DIR") or (Path(__file__).resolve().parents[1] / "runs"))

# Storage backend (scalability seam, see runs §). ``fs`` keeps every run manifest
# and artifact on the local filesystem — correct for tests and single-machine dev,
# but per-instance and ephemeral on Cloud Run. ``cloud`` persists manifests to the
# ``gd_runs`` Firestore collection and artifacts to GCS (via ``app.services``), so
# state is shared across instances and survives redeploys. Default is ``fs`` so the
# offline test suite and the standalone package are unchanged; Cloud Run sets
# ``GD_STORAGE_BACKEND=cloud``. App-service imports stay lazy (inside the cloud
# branch) so the package still imports without the backend app installed.
GD_STORAGE_BACKEND = (os.environ.get("GD_STORAGE_BACKEND") or "fs").strip().lower()

# GCS object prefix for this agent's artifacts: ``generated/gd/<run_id>/...``.
_GCS_PARTITION = "gd"

STATE_FOR_STAGE_CONFIG = {1: "STAGE1_CONFIG", 2: "STAGE2_CONFIG", 3: "STAGE3_CONFIG", 4: "STAGE4_CONFIG"}
STATE_FOR_STAGE_REVIEW = {1: "STAGE1_REVIEW", 2: "STAGE2_REVIEW", 3: "STAGE3_REVIEW", 4: "STAGE4_REVIEW"}


def _use_cloud() -> bool:
    return GD_STORAGE_BACKEND == "cloud"


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
    # element styles, sub-headings) starts from that brand's identity. Falls back
    # to Legal Soft for None/unknown ids. Imported lazily to avoid an import cycle
    # (registry imports the content modules that ultimately import runs' siblings).
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


def save_artifact(run_id: str, stage: int, variant: str, attempt: int, png: bytes) -> str:
    """Persist a generated PNG and return an opaque reference to it.

    The reference is stored verbatim on the attempt and round-tripped through
    ``read_artifact`` / the router. In ``fs`` mode it is the run-relative path
    (``stage-<n>/<variant>-<attempt>.png``); in ``cloud`` mode it is the GCS
    ``gs://`` URI returned by the shared storage service.
    """
    if _use_cloud():
        from app.services import storage

        # ``_safe_name`` flattens "/" in the object name, so the per-run folder is
        # carried in the partition and the stage is folded into a flat file name.
        gs_uri, _signed = storage.upload_generated(
            partition=f"{_GCS_PARTITION}/{run_id}",
            file_name=f"stage-{stage}-{variant}-{attempt}.png",
            data=png,
            content_type="image/png",
        )
        return gs_uri
    rel = f"stage-{stage}/{variant}-{attempt}.png"
    abspath = run_dir(run_id) / rel
    abspath.parent.mkdir(parents=True, exist_ok=True)
    abspath.write_bytes(png)
    return rel


def read_artifact(run_id: str, ref: str) -> bytes:
    """Read an artifact's bytes from its stored reference (fs path or gs:// URI)."""
    if ref.startswith("gs://"):
        from app.services import storage

        return storage.download_bytes(ref)
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
