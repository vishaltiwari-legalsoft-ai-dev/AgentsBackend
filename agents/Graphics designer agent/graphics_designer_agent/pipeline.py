"""Pipeline state machine (spec §4) — generate / approve / back, with mandatory
image chaining (§2.5) and the deterministic Stage-4 composite (§5.4).
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict

from . import variants
from .compositor import composite_logo, logo_placement
from .prompts import load_prompt
from .providers import ImageProvider, get_provider
from .runs import (
    STATE_FOR_STAGE_CONFIG,
    STATE_FOR_STAGE_REVIEW,
    artifact_abspath,
    now_iso,
    save_artifact,
    save_run,
)
from .tokens import ASPECT_RATIOS, DEFAULT_AR, substitute_stage2, substitute_stage3


class PipelineError(Exception):
    """Raised for invalid pipeline transitions (mapped to HTTP 409/400)."""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _stage_dims(run: dict, stage: int) -> tuple[int, int]:
    if stage == 1:
        return (1920, 1080)  # Stage 1 prompts are always 16:9 (§6.2)
    ar = ASPECT_RATIOS.get(run["config"]["aspect_ratio"], ASPECT_RATIOS[DEFAULT_AR])
    return (ar["w"], ar["h"])


def _approved_png(run: dict, stage: int) -> bytes | None:
    appr = run["stages"][str(stage)]["approved"]
    if not appr:
        return None
    return artifact_abspath(run["id"], appr["artifact"]).read_bytes()


def reference_for(run: dict, stage: int) -> list[tuple[bytes, str]] | None:
    """The approved upstream image that MUST be chained into this stage."""
    if stage <= 1:
        return None
    png = _approved_png(run, stage - 1)
    return [(png, "image/png")] if png is not None else None


def build_prompt(run: dict, stage: int, variant: str) -> dict:
    """Return the exact final prompt + audit diff for a stage (no generation)."""
    cfg = run["config"]
    ar = cfg["aspect_ratio"]
    diffs: list = []
    warnings: list[str] = []
    negative: str | None = None

    if stage == 1:
        text = load_prompt(variants.stage1_variant(variant)["prompt_file"])
    elif stage == 2:
        v = variants.stage2_variant(variant)
        sub = substitute_stage2(
            load_prompt(variants.STAGE2_BLEND_PROMPT), variant, ar, subject=v["subject"]
        )
        text, diffs, warnings = sub.text, sub.diffs, list(sub.warnings)
    elif stage == 3:
        tk = cfg["tokens"]
        sub = substitute_stage3(
            load_prompt("stage3_text_overlay.txt"),
            headline=tk["headline"], highlight=tk["highlight"],
            subtext1=tk["subtext1"], subtext2=tk["subtext2"], cta=tk["cta"],
            font=cfg["font"],
            text_placement=variants.text_placement_phrase(cfg.get("text_placement", "left")),
            cta_placement=variants.cta_placement_phrase(cfg.get("cta_placement", "bottom")),
        )
        text, diffs, warnings = sub.text, sub.diffs, list(sub.warnings)
    elif stage == 4:
        text = load_prompt("stage4_logo_composite.txt")
    else:
        raise PipelineError(f"invalid stage {stage}")

    return {
        "text": text,
        "diffs": [asdict(d) for d in diffs],
        "warnings": warnings,
        "negative_prompt": negative,
    }


def generate(run: dict, stage: int, variant: str | None = None,
             provider: ImageProvider | None = None) -> dict:
    """Generate an attempt for stage 1–3; chains the approved upstream image."""
    if stage == 4:
        raise PipelineError("Use generate_stage4 for the logo stage.")
    provider = provider or get_provider()
    key = str(stage)

    if stage in (1, 2):
        variant = (variant or run["stages"][key]["variant"] or "A").upper()
    else:
        variant = "T"  # canonical text overlay — single template

    refs = reference_for(run, stage)
    if stage > 1 and not refs:
        raise PipelineError(f"Stage {stage} requires the approved Stage {stage - 1} image.")

    built = build_prompt(run, stage, variant)
    if built["negative_prompt"] and not provider.supports_negative:
        built["warnings"].append(
            "Provider has no negative-prompt support — Prompt B negative was skipped."
        )

    w, h = _stage_dims(run, stage)
    attempt_no = len(run["stages"][key]["attempts"]) + 1
    label = f"STAGE {stage} · {variant} · #{attempt_no}" if provider.name == "mock" else ""
    png, _mime = provider.generate(
        built["text"],
        reference_images=refs,
        width=w, height=h,
        negative_prompt=built["negative_prompt"] if provider.supports_negative else None,
        label=label,
    )
    rel = save_artifact(run["id"], stage, variant, attempt_no, png)
    attempt = {
        "attempt": attempt_no,
        "variant": variant,
        "artifact": rel,
        "prompt": built["text"],
        "prompt_hash": _sha(built["text"]),
        "diffs": built["diffs"],
        "warnings": built["warnings"],
        "provider": provider.name,
        "created_at": now_iso(),
    }
    st = run["stages"][key]
    st["attempts"].append(attempt)
    st["variant"] = variant
    run["state"] = STATE_FOR_STAGE_REVIEW[stage]
    save_run(run)
    return attempt


def generate_stage4(run: dict, logo_png: bytes, *, use_ai: bool | None = None,
                    provider: ImageProvider | None = None) -> dict:
    """Composite the logo onto the approved Stage-3 image."""
    base = _approved_png(run, 3)
    if base is None:
        raise PipelineError("Stage 4 requires the approved Stage 3 image.")
    use_ai = run["config"]["use_ai_compositor"] if use_ai is None else use_ai
    attempt_no = len(run["stages"]["4"]["attempts"]) + 1

    logo_rel = save_artifact(run["id"], 4, "logo", attempt_no, logo_png)
    if use_ai:
        provider = provider or get_provider()
        text = load_prompt("stage4_logo_composite.txt")
        png, _ = provider.generate(
            text,
            reference_images=[(base, "image/png"), (logo_png, "image/png")],
            label=f"STAGE 4 · final · #{attempt_no}",
        )
        method = "ai"
    else:
        png = composite_logo(base, logo_png)
        method = "deterministic"

    rel = save_artifact(run["id"], 4, "final", attempt_no, png)
    attempt = {
        "attempt": attempt_no,
        "variant": "final",
        "artifact": rel,
        "logo_artifact": logo_rel,
        "method": method,
        "created_at": now_iso(),
    }
    st = run["stages"]["4"]
    st["attempts"].append(attempt)
    st["variant"] = "final"
    run["logo"] = {"artifact": logo_rel}
    run["state"] = STATE_FOR_STAGE_REVIEW[4]
    save_run(run)
    return attempt


def approve(run: dict, stage: int, attempt_no: int | None = None) -> dict:
    st = run["stages"][str(stage)]
    if not st["attempts"]:
        raise PipelineError("Nothing to approve for this stage.")
    if attempt_no is None:
        attempt_no = st["attempts"][-1]["attempt"]
    chosen = next((a for a in st["attempts"] if a["attempt"] == attempt_no), None)
    if not chosen:
        raise PipelineError(f"Attempt {attempt_no} not found.")
    st["approved"] = {
        "attempt": chosen["attempt"],
        "variant": chosen["variant"],
        "artifact": chosen["artifact"],
    }
    run["state"] = STATE_FOR_STAGE_CONFIG[stage + 1] if stage < 4 else "DONE"
    save_run(run)
    return run


def go_back(run: dict, target_stage: int) -> dict:
    """Return to a stage; invalidates all downstream approvals (spec §4)."""
    for n in range(target_stage + 1, 5):
        run["stages"][str(n)]["approved"] = None
    st = run["stages"][str(target_stage)]
    run["state"] = (
        STATE_FOR_STAGE_REVIEW[target_stage] if st["attempts"] else STATE_FOR_STAGE_CONFIG[target_stage]
    )
    save_run(run)
    return run


def stage4_logo_preview(run: dict, logo_w: int, logo_h: int) -> dict:
    """Bounding box the deterministic compositor will use (for the UI preview)."""
    w, h = _stage_dims(run, 4)
    return logo_placement(w, h, logo_w, logo_h)
