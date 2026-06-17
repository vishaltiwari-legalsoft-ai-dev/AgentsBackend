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
from .tokens import (
    DEFAULT_AR,
    DEFAULT_CTA,
    DEFAULT_CTA_PLACEMENT,
    DEFAULT_FONT,
    DEFAULT_HEADLINE,
    DEFAULT_HIGHLIGHT,
    DEFAULT_TEXT_PLACEMENT,
)
from .variants import default_stage3_styles, default_subheadings

RUNS_ROOT = Path(os.environ.get("GD_RUNS_DIR") or (Path(__file__).resolve().parents[1] / "runs"))

STATE_FOR_STAGE_CONFIG = {1: "STAGE1_CONFIG", 2: "STAGE2_CONFIG", 3: "STAGE3_CONFIG", 4: "STAGE4_CONFIG"}
STATE_FOR_STAGE_REVIEW = {1: "STAGE1_REVIEW", 2: "STAGE2_REVIEW", 3: "STAGE3_REVIEW", 4: "STAGE4_REVIEW"}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_stage() -> dict:
    return {"variant": None, "attempts": [], "approved": None}


def create_run(user_id: str, brand_id: str | None = None) -> dict:
    run = {
        "id": uuid.uuid4().hex[:12],
        "user_id": user_id,
        "brand_id": brand_id,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "state": "STAGE1_CONFIG",
        "config": {
            "font": DEFAULT_FONT,
            "aspect_ratio": DEFAULT_AR,
            "text_placement": DEFAULT_TEXT_PLACEMENT,
            "cta_placement": DEFAULT_CTA_PLACEMENT,
            # Per-element Stage-3 styling for the deterministic renderer: headline +
            # CTA carry font/colour/size/placement/pixel-nudge; highlight is inline
            # (font + colour). Sub-headings are the dynamic list below.
            "element_styles": default_stage3_styles(),
            # Stage-3 sub-heading lines (1–5). Each carries its own text + styling.
            "subheadings": default_subheadings(),
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
            # Headline/highlight/CTA text. Sub-heading text lives in ``subheadings``.
            "tokens": {
                "headline": DEFAULT_HEADLINE,
                "highlight": DEFAULT_HIGHLIGHT,
                "cta": DEFAULT_CTA,
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
    d = run_dir(run["id"])
    d.mkdir(parents=True, exist_ok=True)
    run_json_path(run["id"]).write_text(json.dumps(run, indent=2, ensure_ascii=False), encoding="utf-8")


def get_run(run_id: str) -> dict | None:
    path = run_json_path(run_id)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_artifact(run_id: str, stage: int, variant: str, attempt: int, png: bytes) -> str:
    """Persist a generated PNG and return its run-relative path."""
    rel = f"stage-{stage}/{variant}-{attempt}.png"
    abspath = run_dir(run_id) / rel
    abspath.parent.mkdir(parents=True, exist_ok=True)
    abspath.write_bytes(png)
    return rel


def artifact_abspath(run_id: str, rel: str) -> Path:
    # Guard against path traversal.
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
