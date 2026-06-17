"""Pipeline state machine (spec §4) — generate / approve / back, with mandatory
image chaining (§2.5) and the deterministic Stage-4 composite (§5.4).
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict

from . import text_overlay, variants
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
from .tokens import (
    ASPECT_RATIOS,
    DEFAULT_AR,
    DEFAULT_CTA_PLACEMENT,
    DEFAULT_FONT,
    DEFAULT_TEXT_PLACEMENT,
    substitute_stage1,
    substitute_stage2,
)

# Resolution requested from the image model per stage (OpenRouter image_config
# image_size). Stage 1 is a smooth gradient base that gets re-rendered downstream,
# so 2K is plenty; Stages 2-4 carry the photographic subject, text and the final
# deliverable, so they render at the full 4K the brand requires. The model
# defaults to 1K when this is unset — the cause of the soft, low-res output.
STAGE_IMAGE_SIZE = {1: "2K", 2: "4K", 3: "4K", 4: "4K"}


class PipelineError(Exception):
    """Raised for invalid pipeline transitions (mapped to HTTP 409/400)."""


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _stage_ar(run: dict) -> str:
    """The AR token the user selected for this run (validated against presets)."""
    ar = run["config"]["aspect_ratio"]
    return ar if ar in ASPECT_RATIOS else DEFAULT_AR


def _resolve_overlay_spec(run: dict) -> dict:
    """Build the deterministic Stage-3 renderer spec from the run config.

    Resolves the headline (+ inline highlight), the dynamic sub-heading list and
    the CTA into the shape ``text_overlay.render_overlay`` expects. Tolerant of
    older runs that still carry the legacy ``subtext1``/``subtext2`` tokens."""
    cfg = run["config"]
    tk = cfg.get("tokens") or {}
    styles = cfg.get("element_styles") or {}
    base_font = cfg.get("font") or DEFAULT_FONT
    sizes = variants.DEFAULT_TEXT_SIZE_PCT

    def off(s: dict) -> tuple[int, int]:
        return (int(s.get("offset_x", 0) or 0), int(s.get("offset_y", 0) or 0))

    hs = styles.get("headline") or {}
    his = styles.get("highlight") or {}
    cs = styles.get("cta") or {}

    headline = {
        "text": tk.get("headline", ""), "highlight": tk.get("highlight", ""),
        "font": hs.get("font") or base_font,
        "size_pct": float(hs.get("size_pct", sizes["headline"])),
        "color": hs.get("color", "dark"),
        "highlight_color": his.get("color", "gradient"),
        "placement": hs.get("placement", DEFAULT_TEXT_PLACEMENT),
        "offset": off(hs),
    }

    raw_subs = cfg.get("subheadings")
    if raw_subs is None:  # legacy run — fall back to the old two subtext tokens
        raw_subs = [{"text": tk.get("subtext1", "")}, {"text": tk.get("subtext2", "")}]
    subheadings = [
        {
            "text": s["text"], "font": s.get("font") or base_font,
            "size_pct": float(s.get("size_pct", sizes["subheading"])),
            "color": s.get("color", "dark"),
            "placement": s.get("placement", DEFAULT_TEXT_PLACEMENT),
            "offset": off(s),
        }
        for s in raw_subs if (s.get("text") or "").strip()
    ]

    cta = {
        "text": tk.get("cta", ""), "font": cs.get("font") or base_font,
        "size_pct": float(cs.get("size_pct", sizes["cta"])),
        "placement": cs.get("placement", DEFAULT_CTA_PLACEMENT),
        "offset": off(cs),
    }
    return {"headline": headline, "subheadings": subheadings, "cta": cta}


def _stage_dims(run: dict, stage: int) -> tuple[int, int]:
    # Every stage now honours the user's selected aspect ratio — including Stage 1,
    # which previously hard-locked to 16:9 (§6.2).
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
        if variant.upper() == "AI":
            # Temporary AI gradient — its prompt lives on the run config only (never
            # in prompts/ or CANONICAL_SHA256). It still flows through the same AR
            # substitution as the canonical variants.
            custom = cfg.get("custom_gradient") or {}
            template = custom.get("prompt")
            if not template:
                raise PipelineError("Generate an AI gradient first.")
        else:
            template = load_prompt(variants.stage1_variant(variant)["prompt_file"])
        sub = substitute_stage1(template, ar)
        text, diffs, warnings = sub.text, sub.diffs, list(sub.warnings)
    elif stage == 2:
        if variant.upper() == "AI":
            # Temporary AI element — its subject lives on the run config only (never
            # added to STAGE2_VARIANTS). It blends through the same shared prompt.
            custom = cfg.get("custom_element") or {}
            subject = custom.get("subject")
            if not subject:
                raise PipelineError("Generate an AI element first.")
        else:
            subject = variants.stage2_variant(variant)["subject"]
        sub = substitute_stage2(
            load_prompt(variants.STAGE2_BLEND_PROMPT), variant, ar, subject=subject
        )
        text, diffs, warnings = sub.text, sub.diffs, list(sub.warnings)
    elif stage == 3:
        # Stage 3 is rendered deterministically (text_overlay), not by the model,
        # so the "prompt" shown in the audit panel is a readable layout summary.
        text = text_overlay.overlay_spec_summary(_resolve_overlay_spec(run))
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


def _generate_stage3(run: dict) -> dict:
    """Render the Stage-3 text overlay deterministically onto the approved Stage-2
    image (no model call) — exact sizes, positions, colours; base pixels intact."""
    base = _approved_png(run, 2)
    if base is None:
        raise PipelineError("Stage 3 requires the approved Stage 2 image.")
    spec = _resolve_overlay_spec(run)
    w, h = _stage_dims(run, 3)
    png = text_overlay.render_overlay(base, spec, w, h)
    summary = text_overlay.overlay_spec_summary(spec)
    attempt_no = len(run["stages"]["3"]["attempts"]) + 1
    rel = save_artifact(run["id"], 3, "T", attempt_no, png)
    attempt = {
        "attempt": attempt_no,
        "variant": "T",
        "artifact": rel,
        "prompt": summary,
        "prompt_hash": _sha(summary),
        "diffs": [],
        "warnings": [],
        "provider": "deterministic",
        "created_at": now_iso(),
    }
    st = run["stages"]["3"]
    st["attempts"].append(attempt)
    st["variant"] = "T"
    run["state"] = STATE_FOR_STAGE_REVIEW[3]
    save_run(run)
    return attempt


def generate(run: dict, stage: int, variant: str | None = None,
             provider: ImageProvider | None = None) -> dict:
    """Generate an attempt for stage 1–3; chains the approved upstream image."""
    if stage == 4:
        raise PipelineError("Use generate_stage4 for the logo stage.")
    if stage == 3:
        # Deterministic text overlay — no image model, no upstream prompt.
        return _generate_stage3(run)
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
        aspect_ratio=_stage_ar(run),
        image_size=STAGE_IMAGE_SIZE[stage],
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

    layout = run["config"].get("logo_layout") or {}
    logo_rel = save_artifact(run["id"], 4, "logo", attempt_no, logo_png)
    if use_ai:
        provider = provider or get_provider()
        text = load_prompt("stage4_logo_composite.txt")
        hint = _logo_placement_hint(layout)
        if hint:
            text = f"{text}\n\nLOGO PLACEMENT (follow precisely):\n{hint}"
        w, h = _stage_dims(run, 4)
        png, _ = provider.generate(
            text,
            reference_images=[(base, "image/png"), (logo_png, "image/png")],
            width=w, height=h,
            label=f"STAGE 4 · final · #{attempt_no}",
            aspect_ratio=_stage_ar(run),
            image_size=STAGE_IMAGE_SIZE[4],
        )
        method = "ai"
    else:
        png = composite_logo(base, logo_png, layout)
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


def _logo_placement_hint(layout: dict) -> str:
    """A short natural-language placement instruction for the AI compositor path."""
    if not layout:
        return ""
    pos = (layout.get("position") or "top-left").replace("-", " ")
    size = layout.get("size_pct")
    size_txt = f"about {round(size)}% of the image width" if size else "about 20% of the image width"
    bits = [
        f"Place the logo in the {pos} of the image, sized {size_txt}, with a comfortable edge margin.",
        "Preserve the logo's exact colours, shapes and proportions — do not redraw or restyle it.",
    ]
    return " ".join(bits)


def stage4_logo_preview(run: dict, logo_w: int, logo_h: int) -> dict:
    """Bounding box the deterministic compositor will use (for the UI preview)."""
    w, h = _stage_dims(run, 4)
    layout = run["config"].get("logo_layout") or {}
    return logo_placement(
        w, h, logo_w, logo_h,
        position=layout.get("position", "top-left"),
        size_pct=layout.get("size_pct"),
        margin_pct=layout.get("margin_pct"),
        offset_x=int(layout.get("offset_x", 0) or 0),
        offset_y=int(layout.get("offset_y", 0) or 0),
    )
