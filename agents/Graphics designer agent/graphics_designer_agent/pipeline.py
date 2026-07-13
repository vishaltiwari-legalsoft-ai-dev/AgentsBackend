"""Pipeline state machine (spec §4) — generate / approve / back, with mandatory
image chaining (§2.5) and the deterministic Stage-4 composite (§5.4).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import asdict
from io import BytesIO

from . import registry
from .providers import ImageProvider, get_provider
from .runs import (
    STATE_FOR_STAGE_CONFIG,
    STATE_FOR_STAGE_REVIEW,
    create_run,
    now_iso,
    read_artifact,
    save_artifact,
    save_run,
)
from .stage1_gradient import substitute_stage1
from .stage2_element import place_subject, substitute_stage2
from .stage3_text import layout as gd_layout
from .stage3_text import render, text_overlay
from .stage3_text.style_options import DEFAULT_TEXT_SIZE_PCT
from .stage4_logo.compositor import composite_logo, logo_placement
from .tokens import (
    ASPECT_RATIOS,
    DEFAULT_AR,
    DEFAULT_CTA_PLACEMENT,
    DEFAULT_FONT,
    DEFAULT_TEXT_PLACEMENT,
)

# Resolution requested from the image model per stage (OpenRouter image_config
# image_size). Two profiles, selected by GD_IMAGE_QUALITY:
#
#   "social" (DEFAULT) — tuned for social delivery (~1080px feeds): a 1K gradient
#     base + 2K photo. 2K (2048px) is already ~2x what the feed shows and keeps the
#     Pro image model fast. Stage 3 never calls the model (deterministic overlay);
#     Stage 4's size only applies on the AI-compositor path.
#   "max" — print/large-format grade: 2K base + 4K downstream. Noticeably slower.
#
# Explicitly setting a size matters regardless of profile: the model defaults to
# 1K when image_size is unset, which is the real cause of "soft" output.
_IMAGE_QUALITY = (os.environ.get("GD_IMAGE_QUALITY") or "social").strip().lower()
STAGE_IMAGE_SIZE = (
    {1: "2K", 2: "4K", 3: "4K", 4: "4K"}
    if _IMAGE_QUALITY == "max"
    else {1: "1K", 2: "2K", 3: "2K", 4: "2K"}
)

# References (e.g. the Stage-1 gradient chained into Stage 2) only need to convey
# composition + colour, not pixel detail — so we downscale them before upload.
# This trims request size + the model's ingest time without affecting the fresh
# output the model generates.
REFERENCE_MAX_SIDE = 1024

# Upper bound on the deterministic render width (px). The Stage-2 photo can come
# back at the model's full 4K; we keep that resolution through the Stage-3 text
# overlay and the Stage-4 logo composite instead of crushing it to the 1080-px AR
# preset (the old behaviour, which downsized a 4K base — and any high-res logo —
# back to ~1080 before compositing). Bounded so a pathologically large source
# can't blow up memory.
MAX_RENDER_WIDTH = 4096

# This pipeline IS the Graphic Designer agent ("a1" in the agent catalog). The id
# lets the provider resolve this agent's per-agent image-model override set by the
# creator in the Agent Configuration panel, falling back to the global default.
GD_AGENT_ID = "a1"

# The studio editor produces vertical social posts, so its reference precedent is
# drawn from the social-story bucket of the Brand Reference Library.
STUDIO_CREATIVE_TYPE = "social_story"


def _reference_grounding(run: dict) -> str:
    """On-brand reference precedent for this run, as a prompt-ready block.

    Looks up the run's brand + the studio creative type in the Brand Reference
    Library (Drive-synced precedent) and returns a grounding block to append to
    the Stage-2 scene prompt. Fully best-effort: any failure — library not
    importable, empty index, no GCS — returns ``""`` and never breaks generation.
    """
    try:
        from . import reference_library as rl

        records = rl.load_index(rl.default_base_dir())
        if not records:
            return ""
        cfg = run.get("config") or {}
        tk = cfg.get("tokens") or {}
        brief = " ".join(
            str(tk.get(k, "")) for k in ("headline", "highlight", "subtext1", "subtext2", "cta")
        ).strip()
        hits = rl.retrieve_for_generation(
            records,
            brand_id=run.get("brand_id"),
            creative_type=STUDIO_CREATIVE_TYPE,
            brief=brief,
            k=3,
            style_k=2,
        )
        if not hits:
            return ""
        return rl.summarize_for_prompt(hits)
    except Exception:  # noqa: BLE001 - grounding is additive; never fail generation
        return ""


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
    pack = registry.get_pack(run.get("brand_id"))
    tk = cfg.get("tokens") or {}
    styles = cfg.get("element_styles") or {}
    base_font = cfg.get("font") or pack.default_font
    sizes = DEFAULT_TEXT_SIZE_PCT

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
        "align": hs.get("align"),
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
            "align": s.get("align"),
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
    return read_artifact(run["id"], appr["artifact"])


def reference_for(run: dict, stage: int) -> list[tuple[bytes, str]] | None:
    """The approved upstream image that MUST be chained into this stage."""
    if stage <= 1:
        return None
    png = _approved_png(run, stage - 1)
    return [(png, "image/png")] if png is not None else None


def _shrink_reference(refs: list[tuple[bytes, str]] | None,
                      max_side: int = REFERENCE_MAX_SIDE) -> list[tuple[bytes, str]] | None:
    """Downscale reference images so the longest side is ``max_side`` px.

    The reference only guides composition/colour, so a smaller copy gives the
    same result with a smaller payload + faster model ingest. Best-effort: any
    failure falls back to the original bytes."""
    if not refs:
        return refs
    from PIL import Image

    out: list[tuple[bytes, str]] = []
    for data, mime in refs:
        try:
            img = Image.open(BytesIO(data))
            longest = max(img.size)
            if longest > max_side:
                scale = max_side / longest
                img = img.resize(
                    (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
                    Image.LANCZOS,
                )
                buf = BytesIO()
                img.save(buf, format="PNG")
                out.append((buf.getvalue(), "image/png"))
            else:
                out.append((data, mime))
        except Exception:  # noqa: BLE001 - never let a reference tweak break generation
            out.append((data, mime))
    return out


def build_prompt(run: dict, stage: int, variant: str) -> dict:
    """Return the exact final prompt + audit diff for a stage (no generation)."""
    cfg = run["config"]
    pack = registry.get_pack(run.get("brand_id"))
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
            template = pack.load_prompt(pack.stage1_variant(variant)["prompt_file"])
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
            subject = pack.stage2_variant(variant)["subject"]
        # Prompt-steered placement: when the user picks one of the 9 cells we
        # append an explicit override clause; "auto"/absent is a strict no-op.
        subject = place_subject(subject, cfg.get("element_placement"))
        sub = substitute_stage2(
            pack.load_prompt(pack.stage2_blend_prompt), variant, ar, subject=subject
        )
        text, diffs, warnings = sub.text, sub.diffs, list(sub.warnings)
        # Ground the scene on real, on-brand reference precedent (Drive-synced
        # Brand Reference Library) when any is indexed for this brand/type.
        grounding = _reference_grounding(run)
        if grounding:
            text = f"{text}\n\n{grounding}"
    elif stage == 3:
        # Stage 3 is rendered deterministically (text_overlay), not by the model,
        # so the "prompt" shown in the audit panel is a readable layout summary.
        text = text_overlay.overlay_spec_summary(_resolve_overlay_spec(run))
    elif stage == 4:
        text = pack.load_prompt("stage4_logo_composite.txt")
    else:
        raise PipelineError(f"invalid stage {stage}")

    return {
        "text": text,
        "diffs": [asdict(d) for d in diffs],
        "warnings": warnings,
        "negative_prompt": negative,
    }


def _hires_canvas(base_png: bytes, canvas_w: int, canvas_h: int) -> tuple[int, int, float]:
    """Target render size that preserves the approved image's resolution.

    Keeps the locked aspect ratio (``canvas_w``×``canvas_h`` shape) but scales the
    pixel dimensions up to the source image's native width — so a 4K Stage-2 photo
    (and the logo composited later) keep full resolution instead of being forced
    back to the 1080-px preset. Never downsizes below the preset and is bounded by
    ``MAX_RENDER_WIDTH``. Returns ``(w, h, px_scale)``."""
    from PIL import Image

    try:
        native_w, _native_h = Image.open(BytesIO(base_png)).size
    except Exception:  # noqa: BLE001 - unreadable base → fall back to the preset
        return canvas_w, canvas_h, 1.0
    scale = max(1.0, min(native_w / canvas_w, MAX_RENDER_WIDTH / canvas_w))
    return round(canvas_w * scale), round(canvas_h * scale), scale


def _generate_stage3(run: dict) -> dict:
    """Render the Stage-3 text overlay deterministically onto the approved Stage-2
    image (no model call) — exact sizes, positions, colours; base pixels intact."""
    base = _approved_png(run, 2)
    if base is None:
        raise PipelineError("Stage 3 requires the approved Stage 2 image.")
    layers = gd_layout.resolve_layers(run)
    canvas_w, canvas_h = _stage_dims(run, 3)
    w, h, px_scale = _hires_canvas(base, canvas_w, canvas_h)
    png = render.render_layers(
        base, layers, w, h, px_scale=px_scale, pack=registry.get_pack(run.get("brand_id")),
        image_loader=lambda ref: read_artifact(run["id"], ref),
    )
    summary = text_overlay.overlay_spec_summary(_resolve_overlay_spec(run))
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


# Width (px) of the live Stage-3 preview. Small enough to render in well under
# ~100ms, while keeping geometry identical to the full output: placement zones,
# margins and font sizes are all percentages of the width, so they are
# resolution-independent — only the pixel nudges need px_scale.
PREVIEW_MAX_WIDTH = 760


def render_stage3_preview(run: dict, *, tokens: dict | None = None,
                          subheading_texts: list[str] | None = None,
                          max_w: int = PREVIEW_MAX_WIDTH) -> bytes:
    """Render the Stage-3 text overlay for the live editor preview.

    Uses the SAME deterministic renderer as ``_generate_stage3`` (so the preview
    is pixel-faithful to what Generate will produce — same fonts, placement
    zones, wrapping and CTA pill) but at a small size and WITHOUT saving an
    attempt. The not-yet-approved headline/CTA text and sub-heading drafts are
    merged in via ``tokens`` / ``subheading_texts`` so the preview reflects the
    user's live edits, not just the last saved state.
    """
    base = _approved_png(run, 2)
    if base is None:
        raise PipelineError("Stage 3 preview requires the approved Stage 2 image.")

    # Layer the live (unsaved) text over the persisted config, then resolve the
    # exact spec the renderer consumes.
    cfg = dict(run["config"])
    if tokens:
        merged = dict(cfg.get("tokens") or {})
        merged.update({k: v for k, v in tokens.items() if v is not None})
        cfg["tokens"] = merged
    if subheading_texts is not None:
        subs = [dict(s) for s in (cfg.get("subheadings") or [])]
        for i, txt in enumerate(subheading_texts):
            if i < len(subs):
                subs[i] = {**subs[i], "text": txt}
        cfg["subheadings"] = subs
    view = {**run, "config": cfg}

    layers = gd_layout.resolve_layers(view)
    canvas_w, canvas_h = _stage_dims(view, 3)
    pw = max(1, min(max_w, canvas_w))
    ph = max(1, round(canvas_h * pw / canvas_w))
    px_scale = pw / canvas_w  # the user's pixel nudges are calibrated to canvas_w
    return render.render_layers(
        base, layers, pw, ph, px_scale=px_scale, pack=registry.get_pack(run.get("brand_id")),
        image_loader=lambda ref: read_artifact(run["id"], ref),
    )


def generate(run: dict, stage: int, variant: str | None = None,
             provider: ImageProvider | None = None,
             extra_references: list[tuple[bytes, str]] | None = None) -> dict:
    """Generate an attempt for stage 1–3; chains the approved upstream image.

    ``extra_references`` are additional reference images (e.g. the real on-brand
    creatives retrieved from the Brand Reference Library) shown to the image model
    ALONGSIDE the chained upstream image, so Stage 1/2 are grounded VISUALLY on
    precedent — not just by a text description. Bytes are never persisted on the
    run (it is JSON-serialised), so they are passed per call.
    """
    if stage == 4:
        raise PipelineError("Use generate_stage4 for the logo stage.")
    if stage == 3:
        # Deterministic text overlay — no image model, no upstream prompt.
        return _generate_stage3(run)
    if stage == 2 and (variant or "").strip().upper() == "UPLOAD":
        # Composite mode: the user's uploaded subject is pasted onto the
        # approved Stage-1 image deterministically — no image model. Only
        # reachable via variant "UPLOAD"; every other variant keeps the
        # byte-identical AI-generation path.
        return _generate_stage2_composite(run)
    if stage == 1 and (variant or "").strip().upper() == "UPLOAD":
        # The user's uploaded photo IS the background — deterministic cover-fit,
        # no image model. Only reachable via variant "UPLOAD"; every other
        # variant keeps the byte-identical AI-generation path.
        return _generate_stage1_background(run)
    provider = provider or get_provider(agent_id=GD_AGENT_ID)
    key = str(stage)

    if stage in (1, 2):
        variant = (variant or run["stages"][key]["variant"] or "A").upper()
    else:
        variant = "T"  # canonical text overlay — single template

    refs = reference_for(run, stage)
    if stage > 1 and not refs:
        raise PipelineError(f"Stage {stage} requires the approved Stage {stage - 1} image.")
    refs = _shrink_reference(refs)  # smaller upload + faster ingest (Stage 2 base)
    if extra_references:
        # Chained upstream first (it sets composition), then on-brand precedent.
        refs = (refs or []) + (_shrink_reference(extra_references) or [])

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


def _generate_stage2_composite(run: dict) -> dict:
    """Stage-2 attempt from the user's uploaded subject (variant ``UPLOAD``).

    Deterministic: approved Stage-1 image + ``config.subject_asset_ref`` are
    composited with Pillow, honoring the ``element_placement`` grid. No image
    model is involved, so the result is instant and free.
    """
    from .runs import read_artifact
    from .stage2_element.composite import paste_subject

    base = _approved_png(run, 1)
    if base is None:
        raise PipelineError("Stage 2 requires the approved Stage 1 image.")
    ref = (run.get("config") or {}).get("subject_asset_ref")
    if not ref:
        raise PipelineError(
            "Variant UPLOAD needs an uploaded subject — POST /subject/upload first."
        )
    try:
        subject = read_artifact(run["id"], ref)
    except ValueError as exc:
        raise PipelineError(str(exc)) from exc

    png = paste_subject(base, subject, run["config"].get("element_placement"))
    key = "2"
    attempt_no = len(run["stages"][key]["attempts"]) + 1
    rel = save_artifact(run["id"], 2, "UPLOAD", attempt_no, png)
    attempt = {
        "attempt": attempt_no,
        "variant": "UPLOAD",
        "artifact": rel,
        "prompt": "(deterministic composite of the uploaded subject — no image model)",
        "prompt_hash": _sha(f"upload-composite:{ref}"),
        "diffs": [],
        "warnings": [],
        "provider": "upload-composite",
        "method": "deterministic",
        "created_at": now_iso(),
    }
    st = run["stages"][key]
    st["attempts"].append(attempt)
    st["variant"] = "UPLOAD"
    run["state"] = STATE_FOR_STAGE_REVIEW[2]
    save_run(run)
    return attempt


def _generate_stage1_background(run: dict) -> dict:
    """Stage-1 attempt from the user's uploaded photo (variant ``UPLOAD``).

    Deterministic: ``config.background_asset_ref`` is cover-fitted to the run's
    aspect ratio with Pillow — instant and free, no image model."""
    from .stage1_gradient.background import cover_fit

    ref = (run.get("config") or {}).get("background_asset_ref")
    if not ref:
        raise PipelineError(
            "Variant UPLOAD needs an uploaded photo — POST /subject/upload?role=background first."
        )
    try:
        src = read_artifact(run["id"], ref)
    except ValueError as exc:
        raise PipelineError(str(exc)) from exc
    w, h = _stage_dims(run, 1)
    png = cover_fit(src, w, h, max_width=MAX_RENDER_WIDTH)
    attempt_no = len(run["stages"]["1"]["attempts"]) + 1
    rel = save_artifact(run["id"], 1, "UPLOAD", attempt_no, png)
    attempt = {
        "attempt": attempt_no,
        "variant": "UPLOAD",
        "artifact": rel,
        "prompt": "(deterministic cover-fit of the uploaded photo — no image model)",
        "prompt_hash": _sha(f"upload-background:{ref}"),
        "diffs": [],
        "warnings": [],
        "provider": "upload-background",
        "method": "deterministic",
        "created_at": now_iso(),
    }
    st = run["stages"]["1"]
    st["attempts"].append(attempt)
    st["variant"] = "UPLOAD"
    run["state"] = STATE_FOR_STAGE_REVIEW[1]
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
        provider = provider or get_provider(agent_id=GD_AGENT_ID)
        text = registry.get_pack(run.get("brand_id")).load_prompt("stage4_logo_composite.txt")
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


# --------------------------------------------------------------------------- #
# Backbone as a reusable engine for multi-frame / document creative types.
#
# A carousel, blog, brochure or deck is NOT a separate generation system — it is
# the SAME four steps, run to establish one shared on-brand "system" (Stage 1
# foundation + Stage 2 subject), then Stage 3 (text) + Stage 4 (logo) applied per
# frame/page/slide. This keeps the anti-hallucination backbone intact and reuses
# it for every creative type, and is cost-efficient: the image model is called
# only for the shared base (2 calls), not once per frame.
# --------------------------------------------------------------------------- #

def establish_base(
    brand_id: str | None,
    aspect_ratio: str = "1:1",
    *,
    reference_images: list[tuple[bytes, str]] | None = None,
    stage1_variant: str | None = None,
    stage2_variant: str | None = None,
    subject: str | None = None,
    provider: ImageProvider | None = None,
    user_id: str = "creative-agent",
) -> dict:
    """Run the backbone's Stage 1 (foundation) + Stage 2 (subject) once to create
    the shared visual system a multi-frame creative is built on.

    Returns the run with an approved Stage-2 base. ``reference_images`` (the real
    on-brand creatives) are shown to the image model so the base looks like prior
    work, not a generic gradient.

    Pass ``subject`` (free text) to override the Stage-2 foreground with a custom
    scene — this is how a carousel gets a DISTINCT image per slide while every base
    still shares the same brand Stage-1 foundation/palette. It rides the existing
    custom-element ("AI" variant) path, so no new prompt machinery is needed.
    """
    run = create_run(user_id, brand_id)
    run["config"]["aspect_ratio"] = aspect_ratio if aspect_ratio in ASPECT_RATIOS else DEFAULT_AR
    pack = registry.get_pack(brand_id)
    provider = provider or get_provider(agent_id=GD_AGENT_ID)

    v1 = stage1_variant or (pack.stage1_variants[0]["id"] if pack.stage1_variants else "A")
    generate(run, 1, variant=v1, provider=provider, extra_references=reference_images)
    approve(run, 1)
    if subject and subject.strip():
        run["config"]["custom_element"] = {"subject": subject.strip()}
        v2 = "AI"
    else:
        v2 = stage2_variant or (pack.stage2_variants[0]["id"] if pack.stage2_variants else "A")
    generate(run, 2, variant=v2, provider=provider, extra_references=reference_images)
    approve(run, 2)
    return run


def approved_base_png(run: dict) -> bytes | None:
    """The run's approved Stage-2 base image (the subject on the brand foundation),
    or None. Public accessor so the creative layer can analyse the base — e.g. the
    layout brain inspecting where the subject landed — before the text overlay."""
    return _approved_png(run, 2)


def render_frame_on_base(
    run: dict,
    *,
    headline: str = "",
    highlight: str = "",
    subheadings: list[str] | None = None,
    cta: str = "",
    logo_png: bytes | None = None,
    logo_layout: dict | None = None,
    layout: dict | None = None,
) -> bytes:
    """Apply Stage 3 (deterministic text) + Stage 4 (logo composite) for ONE frame
    onto the run's approved Stage-2 base, and return the finished PNG.

    Pure render — it does NOT append attempts or mutate the run's saved state — so
    it is called once per frame of a carousel/blog/deck without disturbing the
    shared base. Uses the exact same renderer (``text_overlay``) and compositor
    (``composite_logo``) as the interactive Stage 3 / Stage 4.

    ``layout`` (optional) overrides where the text sits for THIS frame —
    ``{"placement": "left|right|top|bottom", "color": "dark|white"}`` — so a
    per-slide layout brain can steer the headline + body into the negative space
    away from the subject. The brand font and highlight gradient are untouched.
    """
    base = _approved_png(run, 2)
    if base is None:
        raise PipelineError("Frame render requires an approved Stage 2 base.")

    cfg = dict(run["config"])
    merged_tokens = dict(cfg.get("tokens") or {})
    merged_tokens.update({"headline": headline, "highlight": highlight, "cta": cta})
    cfg["tokens"] = merged_tokens
    # Per-frame placement/colour from the layout brain (headline + sub-headings).
    sub_overrides: dict = {}
    if layout:
        placement = layout.get("placement")
        color = layout.get("color")
        es = {k: dict(v) for k, v in (cfg.get("element_styles") or {}).items()}
        head = dict(es.get("headline") or {})
        if placement:
            head["placement"] = placement
            sub_overrides["placement"] = placement
        if color:
            head["color"] = color
            sub_overrides["color"] = color
        es["headline"] = head
        cfg["element_styles"] = es
    if subheadings is not None:
        base_style = (cfg.get("subheadings") or [{}])[0]
        cfg["subheadings"] = [
            {**base_style, **sub_overrides, "text": t}
            for t in subheadings if (t or "").strip()
        ]
    view = {**run, "config": cfg}

    layers = gd_layout.resolve_layers(view)
    canvas_w, canvas_h = _stage_dims(view, 3)
    w, h, px_scale = _hires_canvas(base, canvas_w, canvas_h)
    png = render.render_layers(
        base, layers, w, h, px_scale=px_scale, pack=registry.get_pack(run.get("brand_id"))
    )
    if logo_png:
        layout = logo_layout if logo_layout is not None else (run["config"].get("logo_layout") or {})
        png = composite_logo(png, logo_png, layout)
    return png


def brand_logo_png(brand_id: str | None) -> bytes | None:
    """Best-effort fetch of a brand's real logo as PNG bytes for Stage 4.

    Pulls the brand's ingested logo from Firestore/GCS and rasterises it. Returns
    ``None`` on any failure (brand has no logo, backend app/GCS not available,
    offline tests) so the caller cleanly runs Stages 1–3 without a logo.
    """
    pack = registry.get_pack(brand_id)
    fid = getattr(pack, "firestore_brand_id", None)
    if not fid:
        return None
    try:
        from app.services import firestore_repo, imaging, storage

        rec = firestore_repo.find_brand_logo(fid)
        if not rec:
            return None
        data = storage.download_bytes(rec["file_url"])
        return imaging.to_png_logo(data, rec.get("file_name", ""), rec.get("file_type", ""))
    except Exception:  # noqa: BLE001 - logo is optional; never block generation
        return None


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
