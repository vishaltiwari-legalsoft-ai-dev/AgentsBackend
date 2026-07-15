"""Graphics Designer agent — 4-stage ad-creative pipeline API (spec §4–§9).

All endpoints are namespaced under ``/api/gd``. Runs are owned by the
authenticated user; artifacts are streamed back through this router so the
frontend never needs direct storage access.
"""

from __future__ import annotations

import base64
import hashlib
import logging

from fastapi import APIRouter, Body, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app.security import get_current_user
from app.services import firestore_repo, imaging, storage

from graphics_designer_agent import pipeline, registry, stage2_element, suggestions, variants
from graphics_designer_agent.pipeline import PipelineError
from graphics_designer_agent.runs import (
    get_run,
    is_own_artifact_ref,
    log_manifest,
    read_artifact,
    save_artifact,
    save_run,
)
from graphics_designer_agent.stage3_text import elements as elements_mod
from graphics_designer_agent.stage3_text import icons as icons_mod
from graphics_designer_agent.stage3_text import layout as gd_layout
from graphics_designer_agent.stage3_text import placement_brain
from graphics_designer_agent.stage3_text import shapes as shapes_mod
from graphics_designer_agent.stage3_text import style_options as text_opts
from graphics_designer_agent.stage4_logo import options as logo_opts
from graphics_designer_agent.tokens import ASPECT_RATIOS

router = APIRouter()
logger = logging.getLogger("agentos.gd")

# The Graphic Designer is agent "a1" in the catalog — the one live agent today.
# Usage events tag this id so the Home dashboard can break activity down per agent.
GD_AGENT_ID = "a1"
GD_AGENT_CATEGORY = "design"


def _pack_for_run(run: dict):
    """The brand pack backing a run (defaults to Legal Soft for legacy runs)."""
    return registry.get_pack(run.get("brand_id"))


def _log_usage(user: dict, action: str, *, count: int = 1, brand: str | None = None) -> None:
    """Best-effort usage event for the Home dashboard (never breaks the request)."""
    firestore_repo.log_usage_event(
        user_id=str(user["id"]),
        email=str(user.get("email", "")),
        agent_id=GD_AGENT_ID,
        category=GD_AGENT_CATEGORY,
        action=action,
        count=count,
        brand=brand,
    )


# GD runs through 4 stages → status slots A,B,C,D in the run tables.
GD_STAGE_LABELS = ["Stage 1 · Background", "Stage 2 · Subject",
                   "Stage 3 · Copy", "Stage 4 · Logo"]


def _brand_name(run: dict) -> str | None:
    try:
        return _pack_for_run(run).name
    except Exception:  # noqa: BLE001 - brand name is decorative, never fatal
        return None


def _creative_summary(run: dict) -> str:
    """A short human summary of what the run is making (brand · AR · headline)."""
    cfg = run.get("config") or {}
    tokens = cfg.get("tokens") or {}
    bits = [b for b in (_brand_name(run), cfg.get("aspect_ratio")) if b]
    head = (tokens.get("headline") or "").strip()
    base = " · ".join(bits)
    return f"{base} — “{head}”" if head else base


def _start_run(user: dict, run: dict) -> None:
    """Open the Table-1 (per-agent) and Table-2 (master) rows for a new GD run."""
    firestore_repo.start_run(
        run_id=str(run.get("id")),
        agent_id=GD_AGENT_ID,
        agent_name="Graphics Designer",
        user_id=str(user["id"]),
        user=str(user.get("email", "")),
        session_id=str(user.get("session_id") or ""),
        timezone=str(user.get("timezone") or "UTC"),
        brand=_brand_name(run),
        brand_id=run.get("brand_id"),
        stages=GD_STAGE_LABELS,
    )


def _advance_run(
    run: dict,
    *,
    stage: int,
    stage_status: str,
    attempt: dict | None = None,
    run_status: str | None = None,
) -> None:
    """Update a run's per-stage status + master row as a stage is generated,
    approved, or the run completes (stage is 1-based; slot index = stage-1)."""
    asset = None
    if attempt and attempt.get("artifact"):
        rid = str(run.get("id"))
        artifact = attempt["artifact"]
        asset = {
            "stage": stage,
            "variant": attempt.get("variant"),
            "artifact": artifact,
            "url": _artifact_url(rid, artifact),
        }
    firestore_repo.update_run(
        run_id=str(run.get("id")),
        agent_id=GD_AGENT_ID,
        stage_index=stage - 1,
        stage_status=stage_status,
        asset=asset,
        summary=_creative_summary(run),
        run_status=run_status,
    )

def _archive_final_image(user: dict, run: dict) -> None:
    """Archive a COMPLETED run's final creative into the admin Image Library.

    Called when Stage 4 is approved (run state = DONE). Copies the approved
    Stage-4 image to a durable GCS home (``generated/gallery/…``) and records a
    pointer + run context in the ``image_library`` Firestore collection, which
    the admin-only "Image Library" tab renders. Best-effort by design: any
    failure is logged and swallowed so the user's approval never breaks.
    """
    rid = str(run.get("id"))
    try:
        approved = (run.get("stages") or {}).get("4", {}).get("approved") or {}
        ref = approved.get("artifact")
        if not ref:
            return
        item = {
            "run_id": rid,
            "user_id": str(user["id"]),
            "user_email": str(user.get("email", "")),
            "brand_id": run.get("brand_id"),
            "brand": _brand_name(run),
            "summary": _creative_summary(run),
            "headline": ((run.get("config") or {}).get("tokens") or {}).get("headline", ""),
            "aspect_ratio": (run.get("config") or {}).get("aspect_ratio"),
            "artifact": ref,
            "image_gs_uri": ref if ref.startswith("gs://") else None,
            "completed_at": firestore_repo._now(),
        }
        # Give the final image a durable, run-independent home in GCS so the
        # gallery survives any future pruning of per-run artifacts. Skipped
        # when GCS isn't configured (local dev) — the fs artifact still backs
        # the gallery through the admin image proxy.
        if storage.is_configured():
            gs_uri, _signed = storage.upload_generated(
                partition="gallery",
                file_name=f"{rid}-final.png",
                data=read_artifact(rid, ref),
                content_type="image/png",
            )
            item["image_gs_uri"] = gs_uri
        firestore_repo.upsert_gallery_image(item)
    except Exception:  # noqa: BLE001 - archiving must never fail the approval
        logger.exception("GD: failed to archive final image for run %s", rid)


# Headline/highlight/CTA text tokens. Sub-heading text lives in the dynamic
# ``subheadings`` list, each with its own approval, gated separately.
CONTENT_TOKENS = ["headline", "highlight", "cta"]


def _stage3_ready(cfg: dict) -> bool:
    """True when every Stage-3 content token AND every sub-heading is approved."""
    if not all(cfg["tokens_approved"].get(t) for t in CONTENT_TOKENS):
        return False
    subs = cfg.get("subheadings") or []
    return bool(subs) and all(s.get("approved") for s in subs)


def _valid_size_pct(v) -> float:
    try:
        v = float(v)
    except (TypeError, ValueError):
        raise HTTPException(400, "size_pct must be a number")
    if not text_opts.TEXT_SIZE_PCT_MIN <= v <= text_opts.TEXT_SIZE_PCT_MAX:
        raise HTTPException(
            400, f"size_pct must be between {text_opts.TEXT_SIZE_PCT_MIN} and "
            f"{text_opts.TEXT_SIZE_PCT_MAX}")
    return round(v, 2)


def _valid_offset(v, axis: str) -> int:
    try:
        v = int(round(float(v)))
    except (TypeError, ValueError):
        raise HTTPException(400, f"{axis} must be an integer")
    if abs(v) > text_opts.TEXT_OFFSET_PX_RANGE:
        raise HTTPException(400, f"{axis} out of range")
    return v


def _valid_color(v) -> str:
    """A named swatch (dark/white/gradient/cta) or a #RRGGBB hex — full palette."""
    if gd_layout.is_valid_color(v):
        return v
    raise HTTPException(400, f"Unknown colour '{v}' (use a named swatch or #RRGGBB)")


# ── serialization ─────────────────────────────────────────────────────────────
def _artifact_url(run_id: str, ref: str) -> str:
    """Browser-facing URL for a stored artifact reference.

    Cloud artifacts are GCS ``gs://`` URIs → return a short-lived signed URL so the
    browser pulls straight from GCS (re-signed on every serialization, mirroring
    ``storage.rehydrate_result``). Filesystem artifacts keep the API byte-proxy.
    """
    if ref.startswith("gs://"):
        from app.services import storage

        try:
            return storage.signed_url_for_gs_uri(ref)
        except Exception:  # noqa: BLE001 - fall back to the proxy if signing fails
            logger.exception("GD: failed to sign artifact URL for %s", ref)
            return f"/api/gd/runs/{run_id}/artifact/{ref}"
    return f"/api/gd/runs/{run_id}/artifact/{ref}"


def _to_client(run: dict) -> dict:
    out = {k: v for k, v in run.items() if k != "stages"}
    stages = {}
    for n, st in run["stages"].items():
        attempts = [
            {**a, "url": _artifact_url(run["id"], a["artifact"])} for a in st["attempts"]
        ]
        approved = st["approved"]
        if approved:
            approved = {**approved, "url": _artifact_url(run["id"], approved["artifact"])}
        stages[n] = {**st, "attempts": attempts, "approved": approved}
    out["stages"] = stages
    out["tokens_ready"] = _stage3_ready(run["config"])
    return out


def _owned_run(run_id: str, user: dict) -> dict:
    run = get_run(run_id)
    if not run or run.get("user_id") != str(user["id"]):
        raise HTTPException(404, "Run not found")
    return run


def _guard(fn):
    try:
        return fn()
    except PipelineError as exc:
        raise HTTPException(409, str(exc)) from exc


def _apply_element_styles(cfg: dict, incoming: dict, pack) -> None:
    """Validate + merge per-element Stage-3 styling into the run config.

    Each element may set ``font`` (any variant in the brand family), ``color``
    (dark / gradient / white — text elements only) and ``placement`` (text or CTA
    placement key, placeable elements only). Unknown elements/attributes or
    out-of-family values are rejected so the prompt only ever sees valid input.
    """
    elements = {e["key"]: e for e in text_opts.STAGE3_ELEMENTS}
    fonts = set(pack.font_names())
    text_places = {p["key"] for p in text_opts.TEXT_PLACEMENTS}
    cta_places = {p["key"] for p in text_opts.CTA_PLACEMENTS}
    styles = cfg.setdefault("element_styles", {})

    for key, patch in incoming.items():
        meta = elements.get(key)
        if not meta or not isinstance(patch, dict):
            raise HTTPException(400, f"Unknown Stage-3 element '{key}'")
        cur = dict(styles.get(key) or {})
        if "font" in patch:
            # "auto" = Text Optimizer picks the weight from the brand pool at
            # generate time (family still locked by construction).
            if patch["font"] not in fonts and patch["font"] != "auto":
                raise HTTPException(
                    400, f"Font is locked to the {pack.font_family} family; "
                    f"'{patch['font']}' is not an allowed variant.")
            cur["font"] = patch["font"]
        if "color" in patch:
            if not meta["colorable"]:
                raise HTTPException(400, f"Element '{key}' has a locked colour.")
            cur["color"] = _valid_color(patch["color"])
        if "placement" in patch:
            if not meta["placeable"]:
                raise HTTPException(400, f"Element '{key}' has no placement control.")
            allowed = cta_places if meta["placement_kind"] == "cta" else text_places
            if patch["placement"] not in allowed:
                raise HTTPException(400, f"Unknown placement '{patch['placement']}' for '{key}'")
            cur["placement"] = patch["placement"]
        if "align" in patch:
            if patch["align"] not in (None, "left", "center", "right"):
                raise HTTPException(400, "align must be left, center or right")
            if patch["align"] is None:
                cur.pop("align", None)
            else:
                cur["align"] = patch["align"]
        if "size_pct" in patch:
            if not meta.get("sizable"):
                raise HTTPException(400, f"Element '{key}' has no size control.")
            cur["size_pct"] = _valid_size_pct(patch["size_pct"])
        for axis in ("offset_x", "offset_y"):
            if axis in patch:
                if not meta["placeable"]:
                    raise HTTPException(400, f"Element '{key}' has no position nudge.")
                cur[axis] = _valid_offset(patch[axis], axis)
        styles[key] = cur


def _apply_subheadings(cfg: dict, incoming: list, pack) -> None:
    """Validate + replace the full Stage-3 sub-heading list (1–5 lines). Each line
    carries its own text, font (brand family), colour, size %, placement and pixel
    nudge so the deterministic renderer can place it exactly."""
    if not isinstance(incoming, list):
        raise HTTPException(400, "subheadings must be a list")
    if not text_opts.SUBHEADING_MIN <= len(incoming) <= text_opts.SUBHEADING_MAX:
        raise HTTPException(
            400, f"Sub-headings must number {text_opts.SUBHEADING_MIN}–"
            f"{text_opts.SUBHEADING_MAX}.")
    fonts = set(pack.font_names())
    text_places = {p["key"] for p in text_opts.TEXT_PLACEMENTS}
    out: list[dict] = []
    for item in incoming:
        if not isinstance(item, dict):
            raise HTTPException(400, "Each sub-heading must be an object")
        text = str(item.get("text", "")).strip()
        if len(text) > 120:
            raise HTTPException(400, "Sub-heading must be ≤ 120 characters.")
        font = item.get("font") or pack.default_font
        if font not in fonts and font != "auto":
            raise HTTPException(
                400, f"Font is locked to the {pack.font_family} family; "
                f"'{font}' is not allowed.")
        color = _valid_color(item.get("color", "dark"))
        placement = item.get("placement", "left")
        if placement not in text_places:
            raise HTTPException(400, f"Unknown placement '{placement}'")
        align = item.get("align")
        if align not in (None, "left", "center", "right"):
            raise HTTPException(400, "align must be left, center or right")
        out.append({
            "text": text,
            "font": font,
            "color": color,
            "size_pct": _valid_size_pct(item.get("size_pct", text_opts.DEFAULT_TEXT_SIZE_PCT["subheading"])),
            "placement": placement,
            **({"align": align} if align else {}),
            "offset_x": _valid_offset(item.get("offset_x", 0), "offset_x"),
            "offset_y": _valid_offset(item.get("offset_y", 0), "offset_y"),
            "approved": bool(item.get("approved", False)),
        })
    cfg["subheadings"] = out


_CUSTOM_GRADIENT_KEYS = ("id", "cid", "title", "desc", "prompt", "css_gradient", "source")


def _apply_custom_gradient(cfg: dict, patch: dict | None, pack) -> None:
    """Validate + store (or clear) the per-creative temporary AI gradient.

    Passing ``None`` / ``{}`` clears it. Otherwise the prompt is validated with
    the same brand/anchor rules the suggestion layer uses, only whitelisted keys
    are kept, and the variant id is pinned to ``"AI"``. The gradient is stored on
    the run config ONLY — never written to ``prompts/`` or the canonical baseline.
    """
    if not patch:
        cfg["custom_gradient"] = None
        return
    if not isinstance(patch, dict):
        raise HTTPException(400, "custom_gradient must be an object")
    prompt = str(patch.get("prompt") or "")
    errors = suggestions._validate_gradient_prompt(prompt, pack=pack)
    if errors:
        raise HTTPException(400, "Invalid AI gradient: " + "; ".join(errors))
    stored = {k: patch[k] for k in _CUSTOM_GRADIENT_KEYS if k in patch}
    stored["id"] = "AI"
    stored["prompt"] = prompt
    cfg["custom_gradient"] = stored


_CUSTOM_ELEMENT_KEYS = ("id", "cid", "title", "desc", "category", "subject", "source")


def _apply_custom_element(cfg: dict, patch: dict | None) -> None:
    """Validate + store (or clear) the per-creative temporary AI element.

    Passing ``None`` / ``{}`` clears it. Otherwise the subject is validated with the
    same foreground-only rules the suggestion layer uses (no colours / background),
    only whitelisted keys are kept, and the variant id is pinned to ``"AI"``. Stored
    on the run config ONLY — never added to STAGE2_VARIANTS.
    """
    if not patch:
        cfg["custom_element"] = None
        return
    if not isinstance(patch, dict):
        raise HTTPException(400, "custom_element must be an object")
    subject = str(patch.get("subject") or "")
    errors = suggestions._validate_element_subject(subject)
    if errors:
        raise HTTPException(400, "Invalid AI element: " + "; ".join(errors))
    stored = {k: patch[k] for k in _CUSTOM_ELEMENT_KEYS if k in patch}
    stored["id"] = "AI"
    stored["subject"] = subject
    cfg["custom_element"] = stored


def _apply_logo_layout(cfg: dict, patch: dict) -> None:
    """Validate + merge the Stage-4 logo placement controls into the run config."""
    from graphics_designer_agent.stage4_logo.compositor import default_logo_layout

    positions = {p["key"] for p in logo_opts.LOGO_POSITIONS}
    cur = {**default_logo_layout(), **(cfg.get("logo_layout") or {})}

    if "position" in patch:
        if patch["position"] not in positions:
            raise HTTPException(400, f"Unknown logo position '{patch['position']}'")
        cur["position"] = patch["position"]
    if "size_pct" in patch:
        v = patch["size_pct"]
        if v is not None:
            try:
                v = float(v)
            except (TypeError, ValueError):
                raise HTTPException(400, "logo size_pct must be a number")
            if not 1 <= v <= 100:
                raise HTTPException(400, "logo size_pct must be between 1 and 100")
        cur["size_pct"] = v
    if "margin_pct" in patch:
        try:
            m = float(patch["margin_pct"])
        except (TypeError, ValueError):
            raise HTTPException(400, "logo margin_pct must be a number")
        if not 0 <= m <= 25:
            raise HTTPException(400, "logo margin_pct must be between 0 and 25")
        cur["margin_pct"] = m
    for axis in ("offset_x", "offset_y"):
        if axis in patch:
            try:
                cur[axis] = int(round(float(patch[axis])))
            except (TypeError, ValueError):
                raise HTTPException(400, f"logo {axis} must be an integer")
            if abs(cur[axis]) > logo_opts.LOGO_OFFSET_PX_RANGE:
                raise HTTPException(400, f"logo {axis} out of range")
    cfg["logo_layout"] = cur


def _apply_layout(cfg: dict, patch: dict | None) -> None:
    """Validate + merge Stage-3 free-drag coordinates into the run config.

    ``patch`` maps an element id (headline / subheading-N / cta / shape id) to a
    coord entry ``{x,y,w,anchor}``. Each entry is clamped to safe ranges; passing
    an explicit ``null`` for an id removes its pin (the element returns to ``auto``
    legacy placement). Passing ``{}``/``None`` for the whole patch is a no-op.
    """
    if not patch:
        return
    if not isinstance(patch, dict):
        raise HTTPException(400, "layout must be an object")
    cur = dict(cfg.get("layout") or {})
    for elem_id, entry in patch.items():
        if entry is None:
            cur.pop(elem_id, None)
        elif isinstance(entry, dict):
            cur[elem_id] = gd_layout.clamp_entry(entry)
        else:
            raise HTTPException(400, f"layout['{elem_id}'] must be an object or null")
    cfg["layout"] = cur


# ── brand selection (multi-brand hub) ─────────────────────────────────────────
@router.get("/gd/brands")
def gd_brands(_user: dict = Depends(get_current_user)) -> dict:
    """Brands the studio can produce creatives for (drives the left-panel picker)."""
    return {"brands": registry.list_packs(), "default": registry.DEFAULT_BRAND_ID}


def _ingested_logo_url(brand_id: str) -> str | None:
    """Signed view URL for a brand's ingested logo (None if absent/unsignable)."""
    rec = firestore_repo.find_brand_logo(brand_id)
    if not rec:
        return None
    return storage.signed_url_for_gs_uri(rec["file_url"])


def _reference_count(brand_name: str) -> int | None:
    """Indexed reference-creative count for a brand. None = index unavailable
    (the strip renders an em-dash instead of a wrong zero)."""
    from graphics_designer_agent import reference_library as rl

    records = rl.load_index(rl.default_base_dir())
    if not records:
        return None
    needle = (brand_name or "").strip().casefold()
    return sum(1 for r in records if str(r.get("brand_name", "")).strip().casefold() == needle)


def ingested_brand_entries(brands: list[dict], *, logo_url_for, reference_count) -> list[dict]:
    """Strip entries for every brand whose kit data has been ingested (has
    ``brand_metadata.enrichment`` or a baked ``gd_spec``). Pure over its inputs;
    the injected helpers are each best-effort — any raise degrades that field."""
    out: list[dict] = []
    for b in brands:
        meta = b.get("brand_metadata") or {}
        enrichment = meta.get("enrichment") or {}
        if "enrichment" not in meta and "gd_spec" not in meta:
            continue
        try:
            logo_url = logo_url_for(b.get("id"))
        except Exception:  # noqa: BLE001 - decorative; the strip shows an initial instead
            logo_url = None
        counts = {
            "fonts": len(enrichment.get("font_files") or []),
            "logos": len(enrichment.get("logo_files") or []),
        }
        try:
            refs = reference_count(b.get("brand_name") or "")
        except Exception:  # noqa: BLE001 - index unavailable → omit, never guess
            refs = None
        if refs is not None:
            counts["reference_assets"] = refs
        out.append({
            "id": b.get("id"),
            "name": b.get("brand_name"),
            "logo_url": logo_url,
            "primary_colors": list(meta.get("primary_colors") or [])[:6],
            "counts": counts,
            "source": enrichment.get("source"),
        })
    return out


@router.get("/gd/ingested-brands")
def gd_ingested_brands(_user: dict = Depends(get_current_user)) -> dict:
    """Brands whose kit data has been ingested — powers the setup-screen strip.
    Read-only and best-effort: any listing failure returns an empty strip."""
    try:
        brands = firestore_repo.list_brands()
    except Exception:  # noqa: BLE001 - the strip is decorative; empty beats a 500
        logger.exception("GD: ingested-brands listing failed")
        return {"brands": []}
    return {"brands": ingested_brand_entries(
        brands, logo_url_for=_ingested_logo_url, reference_count=_reference_count,
    )}


# ── static config for the studio UI (per selected brand) ───────────────────────
@router.get("/gd/config")
def gd_config(brand: str | None = None, _user: dict = Depends(get_current_user)) -> dict:
    pack = registry.get_pack(brand)
    return {
        "brand_id": pack.id,
        "brand_name": pack.name,
        "stage1_variants": pack.stage1_variants,
        "stage2_variants": pack.stage2_variants,
        "stage2_categories": pack.stage2_categories,
        "stage2_placements": stage2_element.STAGE2_PLACEMENTS,
        "fonts": pack.font_names(),
        "font_family": pack.font_family,
        "font_variants": pack.font_variants,
        "text_placements": text_opts.TEXT_PLACEMENTS,
        "cta_placements": text_opts.CTA_PLACEMENTS,
        "text_colors": pack.text_colors,
        "stage3_elements": text_opts.STAGE3_ELEMENTS,
        "text_size_pct_min": text_opts.TEXT_SIZE_PCT_MIN,
        "text_size_pct_max": text_opts.TEXT_SIZE_PCT_MAX,
        "default_text_size_pct": text_opts.DEFAULT_TEXT_SIZE_PCT,
        "text_offset_px_range": text_opts.TEXT_OFFSET_PX_RANGE,
        "subheading_min": text_opts.SUBHEADING_MIN,
        "subheading_max": text_opts.SUBHEADING_MAX,
        "anchors": list(gd_layout.ANCHORS),
        "shape_kinds": list(shapes_mod.SHAPE_KINDS),
        "icon_keys": list(icons_mod.ICON_KEYS),
        "logo_positions": logo_opts.LOGO_POSITIONS,
        "logo_size_pct_min": logo_opts.LOGO_SIZE_PCT_MIN,
        "logo_size_pct_max": logo_opts.LOGO_SIZE_PCT_MAX,
        "logo_offset_px_range": logo_opts.LOGO_OFFSET_PX_RANGE,
        "aspect_ratios": variants.ASPECT_RATIO_PRESETS,
        "brand_kit_block": pack.brand_kit_block,
        "locked_colors": pack.locked_colors,
        "stage1_source_note": pack.source_note_stage1,
        "onboarding_questions": pack.onboarding_questions,
        "discovery_questions": pack.discovery_questions,
        "content_tokens": CONTENT_TOKENS,
    }


@router.get("/gd/elements")
def gd_elements(_user: dict = Depends(get_current_user)) -> dict:
    """Element Library catalogs for the Stage-3 picker: emoji, rich icons, stickers."""
    return {
        "emoji": elements_mod.emoji_catalog(),
        "icons": elements_mod.icon_catalog(),
        "stickers": elements_mod.sticker_catalog(),
        "max_elements": elements_mod.MAX_ELEMENTS,
    }


@router.get("/gd/fonts/{font_name}")
def gd_font_endpoint(font_name: str, brand: str | None = None,
                     user: dict = Depends(get_current_user)):
    """Serve one brand font file so the V2 editor canvas renders TRUE brand
    typography client-side. Names are validated against the pack's catalog
    (no path traversal); files are immutable, so cache hard."""
    from fastapi import Response

    from graphics_designer_agent import registry

    pack = registry.get_pack(brand)
    if font_name not in set(pack.font_names()):
        raise HTTPException(404, f"Unknown font '{font_name}'")
    path = pack.fonts_dir / pack.font_file(font_name)
    if not path.exists():
        raise HTTPException(404, "Font file missing on the server.")
    return Response(content=path.read_bytes(), media_type="font/otf",
                    headers={"Cache-Control": "public, max-age=86400"})


@router.get("/gd/prompts")
def gd_prompts(brand: str | None = None, _user: dict = Depends(get_current_user)) -> dict:
    """Canonical prompt integrity report (audit panel) for the selected brand."""
    pack = registry.get_pack(brand)
    return {
        "prompts": [
            {"filename": name, "hash": pack.prompt_hash(name), "expected": expected,
             "ok": pack.prompt_hash(name) == expected,
             "bytes": len(pack.load_prompt(name).encode("utf-8"))}
            for name, expected in pack.canonical_sha256.items()
        ]
    }


# ── run lifecycle ─────────────────────────────────────────────────────────────
class CreateRunBody(BaseModel):
    brand_id: str | None = None
    # Optional setup-screen fields (V2 UI) — applied atomically at creation so
    # the strategist + suggestions are grounded from the very first call.
    aspect_ratio: str | None = None
    creative_type: str | None = None
    creative_brief: dict[str, str] | None = None
    remix_enabled: bool | None = None


@router.post("/gd/runs")
def create_run_endpoint(body: CreateRunBody = Body(default=CreateRunBody()),
                        user: dict = Depends(get_current_user)) -> dict:
    from graphics_designer_agent.runs import create_run, save_run

    if body.aspect_ratio is not None and body.aspect_ratio not in ASPECT_RATIOS:
        raise HTTPException(400, f"Unknown aspect ratio '{body.aspect_ratio}'")

    run = create_run(user_id=str(user["id"]), brand_id=body.brand_id)
    changed = False
    if body.aspect_ratio is not None:
        run["config"]["aspect_ratio"] = body.aspect_ratio
        changed = True
    if body.creative_type is not None:
        # Informational for now — this pipeline renders social creatives; other
        # types route to the Creative Agent rail.
        run["config"]["creative_type"] = body.creative_type
        changed = True
    if body.creative_brief:
        brief = dict(run["config"].get("creative_brief") or {})
        for k, v in body.creative_brief.items():
            text = (v or "").strip()
            if text:
                brief[k] = text
        run["config"]["creative_brief"] = brief
        changed = True
    if body.remix_enabled is not None:
        run["config"]["remix_enabled"] = bool(body.remix_enabled)
        changed = True
    if changed:
        save_run(run)
    _log_usage(user, "session", brand=body.brand_id)  # one run = one GD session
    _start_run(user, run)  # open the Table-1 + Table-2 rows for this run
    return _to_client(run)


@router.get("/gd/runs/{run_id}")
def get_run_endpoint(run_id: str, user: dict = Depends(get_current_user)) -> dict:
    return _to_client(_owned_run(run_id, user))


class ConfigBody(BaseModel):
    font: str | None = None
    aspect_ratio: str | None = None
    text_placement: str | None = None
    cta_placement: str | None = None
    # Stage-2 subject placement (prompt-steered): "auto" or one of the 9 cells.
    element_placement: str | None = None
    # Per-element Stage-3 styling: element key -> {font?, color?, placement?, size_pct?, offset_x?, offset_y?}.
    element_styles: dict[str, dict] | None = None
    # Stage-3 sub-heading lines (1–5). Full-list replace.
    subheadings: list[dict] | None = None
    # Stage-3 free-drag coordinates: {element_id: {x,y,w,anchor} | null}.
    layout: dict | None = None
    # Stage-3 shapes / infographic elements (full-list replace).
    shapes: list | None = None
    # Stage-3 rich elements (emoji/icon/sticker/image) — full-list replace.
    elements: list | None = None
    # Stage-4 logo placement: {position?, size_pct?, margin_pct?, offset_x?, offset_y?}.
    logo_layout: dict | None = None
    # Per-creative temporary AI gradient (Stage 1). Explicit null clears it.
    custom_gradient: dict | None = None
    # Per-creative temporary AI element (Stage 2). Explicit null clears it.
    custom_element: dict | None = None
    # Uploaded Stage-2 subject (composite mode, variant "UPLOAD"). Must be an
    # artifact ref owned by this run (from /subject/upload). Explicit null clears.
    subject_asset_ref: str | None = None
    # Uploaded Stage-1 background (cover-fit mode, Stage-1 variant "UPLOAD").
    # Must be an artifact ref owned by this run. Explicit null clears.
    background_asset_ref: str | None = None
    # Pre-generation discovery brief (feeling/audience/tone/style/event/theme).
    # Shallow-merged onto the run so the conversation is durable + reaches every
    # suggestion. Keys with empty values are dropped (lets the UI clear an answer).
    creative_brief: dict | None = None
    use_ai_compositor: bool | None = None
    remix_enabled: bool | None = None
    # Stage-3 Text Optimizer: optional free-text placement/style notes woven into
    # the polish prompts (≤500 chars kept).
    polish_notes: str | None = None
    tokens: dict[str, str] | None = None
    # token -> {approved: bool, source: "user"|"agent", original_suggestion?: str}
    token_approvals: dict[str, dict] | None = None


@router.post("/gd/runs/{run_id}/config")
def update_config(run_id: str, body: ConfigBody, user: dict = Depends(get_current_user)) -> dict:
    run = _owned_run(run_id, user)
    cfg = run["config"]
    pack = _pack_for_run(run)
    if body.font is not None:
        # The creative font is locked to the brand's family — reject anything else.
        if body.font not in set(pack.font_names()):
            raise HTTPException(
                400,
                f"Font is locked to the {pack.font_family} family; "
                f"'{body.font}' is not an allowed variant.",
            )
        cfg["font"] = body.font
    if body.aspect_ratio is not None and body.aspect_ratio != cfg["aspect_ratio"]:
        if body.aspect_ratio not in ASPECT_RATIOS:
            raise HTTPException(400, f"Unknown aspect ratio '{body.aspect_ratio}'")
        # Aspect ratio is chosen at Stage 1 and LOCKED once the run advances past
        # it, so every downstream stage shares one canvas size (spec §6.2). It
        # becomes editable again only by going back to Stage 1 (which invalidates
        # the downstream approvals that depend on the canvas size).
        if not run["state"].startswith("STAGE1"):
            raise HTTPException(
                409,
                "Aspect ratio is locked after Stage 1. Go back to Stage 1 to change it.",
            )
        cfg["aspect_ratio"] = body.aspect_ratio
    if body.text_placement is not None:
        allowed = {p["key"] for p in text_opts.TEXT_PLACEMENTS}
        if body.text_placement not in allowed:
            raise HTTPException(400, f"Unknown text placement '{body.text_placement}'")
        cfg["text_placement"] = body.text_placement
    if body.cta_placement is not None:
        allowed = {p["key"] for p in text_opts.CTA_PLACEMENTS}
        if body.cta_placement not in allowed:
            raise HTTPException(400, f"Unknown CTA placement '{body.cta_placement}'")
        cfg["cta_placement"] = body.cta_placement
    if body.element_placement is not None:
        allowed = {p["key"] for p in stage2_element.STAGE2_PLACEMENTS}
        if body.element_placement not in allowed:
            raise HTTPException(400, f"Unknown subject placement '{body.element_placement}'")
        cfg["element_placement"] = body.element_placement
    if body.element_styles is not None:
        _apply_element_styles(cfg, body.element_styles, pack)
    if body.subheadings is not None:
        _apply_subheadings(cfg, body.subheadings, pack)
    if body.layout is not None:
        _apply_layout(cfg, body.layout)
    if body.shapes is not None:
        try:
            cfg["shapes"] = gd_layout.sanitize_shapes(body.shapes)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    if body.elements is not None:
        try:
            sanitized = elements_mod.sanitize_elements(body.elements)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        # C1 fix: an "image" element's ``ref`` must be an artifact this run owns
        # (e.g. from /gd/runs/{run_id}/elements/upload). Without this check a
        # user could point ``ref`` at another run's — or any SA-readable —
        # ``gs://`` object and have it fetched with the server's credentials
        # at render time (see graphics_designer_agent.runs.is_own_artifact_ref).
        for el in sanitized:
            if el["kind"] == "image" and not is_own_artifact_ref(run_id, el["ref"]):
                raise HTTPException(
                    400, f"Invalid image element ref (not owned by this run): {el['ref']!r}"
                )
        cfg["elements"] = sanitized
    if body.logo_layout is not None:
        _apply_logo_layout(cfg, body.logo_layout)
    # Use the field-set so an explicit ``null`` clears the gradient (omission
    # leaves whatever is already stored untouched).
    if "custom_gradient" in body.model_fields_set:
        _apply_custom_gradient(cfg, body.custom_gradient, pack)
    if "custom_element" in body.model_fields_set:
        _apply_custom_element(cfg, body.custom_element)
    if "subject_asset_ref" in body.model_fields_set:
        if body.subject_asset_ref is None:
            cfg["subject_asset_ref"] = None
        else:
            # Same C1 rule as image elements: the ref must belong to this run.
            if not is_own_artifact_ref(run_id, body.subject_asset_ref):
                raise HTTPException(
                    400, f"Invalid subject ref (not owned by this run): {body.subject_asset_ref!r}"
                )
            cfg["subject_asset_ref"] = body.subject_asset_ref
    if "background_asset_ref" in body.model_fields_set:
        if body.background_asset_ref is None:
            cfg["background_asset_ref"] = None
        else:
            # Same C1 ownership rule as subject/image refs.
            if not is_own_artifact_ref(run_id, body.background_asset_ref):
                raise HTTPException(
                    400, f"Invalid background ref (not owned by this run): {body.background_asset_ref!r}"
                )
            cfg["background_asset_ref"] = body.background_asset_ref
    if body.creative_brief is not None:
        brief = dict(cfg.get("creative_brief") or {})
        for k, v in body.creative_brief.items():
            text = (v or "").strip() if isinstance(v, str) else v
            if text:
                brief[k] = text
            else:
                brief.pop(k, None)  # empty value clears that answer
        cfg["creative_brief"] = brief
    if body.use_ai_compositor is not None:
        cfg["use_ai_compositor"] = bool(body.use_ai_compositor)
    if body.remix_enabled is not None:
        cfg["remix_enabled"] = bool(body.remix_enabled)
    if body.tokens:
        for k, v in body.tokens.items():
            # Known token or an optional detail field (venue/website may be absent
            # on runs created before those fields existed).
            if k in cfg["tokens"] or k in ("venue", "website"):
                cfg["tokens"][k] = v
    if body.token_approvals:
        for token, info in body.token_approvals.items():
            if token not in cfg["tokens_approved"]:
                continue
            approved = bool(info.get("approved"))
            cfg["tokens_approved"][token] = approved
            if approved:
                log_manifest(
                    run, token=token, source=info.get("source", "user"),
                    original_suggestion=info.get("original_suggestion"),
                    final_value=cfg["tokens"].get(token),
                )
    save_run(run)
    return _to_client(run)


# ── generation / approval ─────────────────────────────────────────────────────
class GenerateBody(BaseModel):
    stage: int = Field(ge=1, le=3)
    variant: str | None = None


class TextPreviewBody(BaseModel):
    # Live, not-yet-approved values, layered over the persisted run config so the
    # preview matches what Generate would produce.
    tokens: dict[str, str] | None = None
    subheading_texts: list[str] | None = None


@router.post("/gd/runs/{run_id}/text-preview")
def text_preview_endpoint(run_id: str, body: TextPreviewBody,
                          user: dict = Depends(get_current_user)) -> Response:
    """Live WYSIWYG preview of the Stage-3 text overlay.

    Deterministic (no image model) and unsaved — returns a small PNG rendered by
    the exact same engine as the final Stage-3 output, so the editor shows where
    every element lands and whether it fits.
    """
    run = _owned_run(run_id, user)
    png = _guard(lambda: pipeline.render_stage3_preview(
        run, tokens=body.tokens, subheading_texts=body.subheading_texts))
    return Response(content=png, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/gd/runs/{run_id}/suggest-placement")
def suggest_placement_endpoint(run_id: str, user: dict = Depends(get_current_user)) -> dict:
    """Propose a polished, premium arrangement for the Stage-3 elements present.

    Vision-first: a micro-subagent (``stage3_text.placement_brain``) looks at the
    ACTUAL approved Stage-2 image and judges the clean zone / text colour / image
    density; the deterministic arranger turns that into exact coordinates. If
    vision is unavailable or fails, the arranger runs on Stage-2 metadata alone —
    exactly the previous behaviour (``source: "deterministic"``).

    A one-click refinement: returns ``{layout, element_styles?, ...}`` but does
    NOT persist it. The client previews the proposal and chooses to apply (via
    the config patch) or discard, so the AI arranger never overrides the user's
    flow by default.
    """
    run = _owned_run(run_id, user)
    judgment = None
    base = pipeline.approved_base_png(run)
    if base:
        cfg = run.get("config", {})
        tokens = cfg.get("tokens", {})
        judgment = placement_brain.decide(
            base,
            headline=tokens.get("headline", ""),
            subheading_count=len(cfg.get("subheadings") or []),
            cta=tokens.get("cta", ""),
            element_placement=cfg.get("element_placement"),
        )
    return suggestions.suggest_placement(run, _pack_for_run(run), judgment=judgment)


@router.post("/gd/runs/{run_id}/generate")
def generate_endpoint(run_id: str, body: GenerateBody, user: dict = Depends(get_current_user)) -> dict:
    run = _owned_run(run_id, user)
    if body.stage == 3 and not _stage3_ready(run["config"]):
        raise HTTPException(409, "Approve the headline, highlight, CTA and every sub-heading before generating Stage 3.")
    attempt = _guard(lambda: pipeline.generate(run, body.stage, variant=body.variant))
    _log_usage(user, "generate", brand=run.get("brand_id"))  # one creative produced
    _advance_run(run, stage=body.stage, stage_status="generated", attempt=attempt)
    payload = {"attempt": {**attempt, "url": _artifact_url(run_id, attempt["artifact"])},
               "run": _to_client(run)}
    if body.stage == 3 and attempt.get("set_id"):
        # Text Optimizer set: return all styled siblings so the client can show
        # the 3-up gallery without re-deriving them from the run.
        siblings = [a for a in run["stages"]["3"]["attempts"]
                    if a.get("set_id") == attempt["set_id"]]
        payload["attempts"] = [
            {**a, "url": _artifact_url(run_id, a["artifact"])} for a in siblings
        ]
    return payload


def _local_logo_png(pack) -> bytes | None:
    """PNG bytes for a brand's BUNDLED logo file — the Stage-4 fallback used when
    the brand isn't ingested into Firestore/GCS (e.g. local dev). None if the pack
    has no bundled logo or it can't be read."""
    p = getattr(pack, "logo_path", None)
    if not p or not p.exists():
        return None
    try:
        return imaging.to_png_logo(p.read_bytes(), file_name=p.name, mime="image/png")
    except Exception:  # noqa: BLE001 - unreadable bundled logo → treat as no logo
        logger.exception("GD: failed to read bundled brand logo %s", p)
        return None


def _resolve_logo_variant(pack, logo_id: str | None):
    """Path to a specific bundled logo VARIANT by id (its filename stem), safely
    constrained to the brand's ``logo_dir`` (rejects any path-traversal). None if
    the brand has no library or the id is unknown."""
    d = getattr(pack, "logo_dir", None)
    if not d or not logo_id or "/" in logo_id or "\\" in logo_id or ".." in logo_id:
        return None
    p = d / f"{logo_id}.png"
    try:
        if p.exists() and p.resolve().parent == d.resolve():
            return p
    except Exception:  # noqa: BLE001
        return None
    return None


def _thumb_data_url(path, max_px: int = 200) -> str:
    """A small transparent-PNG thumbnail of a logo file, inline as a data URL."""
    from io import BytesIO

    from PIL import Image

    im = Image.open(path).convert("RGBA")
    im.thumbnail((max_px, max_px), Image.LANCZOS)
    buf = BytesIO()
    im.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _list_brand_logos(pack) -> list[dict]:
    """The brand's on-brand logo variants (from ``logo_dir``) as a pickable
    library: ``[{id, name, thumb}]``. Empty when the brand ships no library."""
    d = getattr(pack, "logo_dir", None)
    out: list[dict] = []
    if not d or not d.exists():
        return out
    for p in sorted(d.glob("*.png")):
        try:
            out.append({"id": p.stem, "name": p.stem.replace("-", " ").title(),
                        "thumb": _thumb_data_url(p)})
        except Exception:  # noqa: BLE001 - skip an unreadable variant, never 500
            logger.exception("GD: failed to thumbnail brand logo %s", p)
    return out


def _brand_logo_png(run: dict, logo_id: str | None = None) -> bytes | None:
    """PNG bytes for the run brand's resolved logo, or None. Resolution order:
    the user-picked variant (``logo_id``) → the brand's Firestore/GCS logo
    (production) → the bundled default. So Stage 4 composites the chosen on-brand
    logo, and still works offline / before the kit is ingested."""
    pack = _pack_for_run(run)
    variant = _resolve_logo_variant(pack, logo_id)
    if variant:
        try:
            png = imaging.to_png_logo(variant.read_bytes(), file_name=variant.name, mime="image/png")
            if png:
                return png
        except Exception:  # noqa: BLE001 - fall through to the other sources
            logger.exception("GD: failed to read picked logo variant %s", variant)
    fb_id = pack.firestore_brand_id
    if fb_id:
        rec = firestore_repo.find_brand_logo(fb_id)
        if rec:
            try:
                raw = storage.download_bytes(rec["file_url"])
                png = imaging.to_png_logo(raw, file_name=rec.get("file_name", ""), mime=rec.get("file_type", ""))
                if png:
                    return png
            except Exception:  # noqa: BLE001 - unreadable remote logo → try local
                logger.exception("GD: failed to download brand logo %s", rec.get("file_url"))
    return _local_logo_png(pack)


@router.get("/gd/runs/{run_id}/brand-logo")
def brand_logo_status(run_id: str, user: dict = Depends(get_current_user)) -> dict:
    """Whether the run's brand has a logo on file, plus a preview URL for the UI.

    Lets Stage 4 default to the brand logo instead of forcing an upload; the
    actual composite re-resolves the logo server-side (never trusts the client).
    """
    run = _owned_run(run_id, user)
    pack = _pack_for_run(run)
    fb_id = pack.firestore_brand_id
    rec = firestore_repo.find_brand_logo(fb_id) if fb_id else None
    if rec:
        view_url = None
        try:
            view_url = storage.signed_url_for_gs_uri(rec["file_url"])
        except Exception:  # noqa: BLE001
            logger.exception("GD: failed to sign brand logo for run %s", run_id)
        return {
            "available": True,
            "view_url": view_url,
            "file_name": rec.get("file_name"),
            "brand_name": pack.name,
        }
    # Fallback: a bundled local logo (dev / brand not yet ingested in Firestore).
    # Returned inline as a data URL so the UI preview needs no extra auth'd fetch.
    local = _local_logo_png(pack)
    if local:
        b64 = base64.b64encode(local).decode("ascii")
        return {
            "available": True,
            "view_url": f"data:image/png;base64,{b64}",
            "file_name": pack.logo_path.name if getattr(pack, "logo_path", None) else "brand-logo.png",
            "brand_name": pack.name,
        }
    return {"available": False, "view_url": None, "file_name": None, "brand_name": pack.name}


@router.get("/gd/runs/{run_id}/brand-logos")
def brand_logo_library(run_id: str, user: dict = Depends(get_current_user)) -> dict:
    """The brand's pickable logo library for Stage 4 — every on-brand variant
    (combined/favicon × gradient/neutral/solid, etc.) with a thumbnail, so the
    user selects the one they like instead of being forced onto a single logo."""
    run = _owned_run(run_id, user)
    pack = _pack_for_run(run)
    return {"logos": _list_brand_logos(pack), "brand_name": pack.name}


@router.post("/gd/runs/{run_id}/stage4")
async def stage4_endpoint(
    run_id: str,
    logo: UploadFile | None = File(default=None),
    use_ai: bool = Form(default=False),
    logo_id: str | None = Form(default=None),
    user: dict = Depends(get_current_user),
) -> dict:
    run = _owned_run(run_id, user)
    # An uploaded file always wins (override); otherwise fall back to the brand's
    # logo from Firestore so the user isn't forced to re-supply it every run.
    png: bytes | None = None
    if logo is not None:
        raw = await logo.read()
        if raw:
            png = imaging.to_png_logo(raw, file_name=logo.filename or "", mime=logo.content_type or "")
            if not png:
                raise HTTPException(415, f"Couldn't read '{logo.filename}' as an image (PNG/JPG/SVG).")
    if png is None:
        png = _brand_logo_png(run, logo_id=logo_id)
    if png is None:
        raise HTTPException(
            400,
            "No logo available — upload one, or pick a brand that has a logo in its kit.",
        )
    attempt = _guard(lambda: pipeline.generate_stage4(run, png, use_ai=use_ai))
    _log_usage(user, "generate", brand=run.get("brand_id"))  # final logo composite
    _advance_run(run, stage=4, stage_status="generated", attempt=attempt)
    return {"attempt": {**attempt, "url": _artifact_url(run_id, attempt["artifact"])}, "run": _to_client(run)}


@router.post("/gd/runs/{run_id}/elements/upload")
async def gd_element_upload(run_id: str, file: UploadFile = File(...),
                            user: dict = Depends(get_current_user)) -> dict:
    """Upload a transparent PNG/WebP to use as an ``image`` element. Returns the
    artifact ref the client stores as the element's ``ref``."""
    run = _owned_run(run_id, user)
    if (file.content_type or "") not in ("image/png", "image/webp"):
        raise HTTPException(400, "Only PNG/WebP uploads are supported.")
    data = await file.read()
    if len(data) > 8 * 1024 * 1024:
        raise HTTPException(400, "Image too large (max 8 MB).")
    # Reuse the run artifact store; stage=3, kind="upload". Name the artifact by
    # a content hash (not a dead upload counter) so distinct images never
    # collide on the same path and identical re-uploads dedupe to one file.
    token = hashlib.sha256(data).hexdigest()[:16]
    rel = save_artifact(run["id"], 3, "upload", token, data)
    return {"ref": rel}


@router.post("/gd/runs/{run_id}/subject/upload")
async def gd_subject_upload(run_id: str, file: UploadFile = File(...),
                            role: str = "subject",
                            user: dict = Depends(get_current_user)) -> dict:
    """Upload an image for this run — the Stage-2 subject (``role=subject``,
    default) or the Stage-1 background (``role=background``). Accepts
    PNG/WebP/JPEG and normalizes to PNG. The ref is stored via config
    ``subject_asset_ref`` / ``background_asset_ref`` and consumed by that
    stage's ``UPLOAD`` variant."""
    if role not in ("subject", "background"):
        raise HTTPException(400, "role must be 'subject' or 'background'")
    run = _owned_run(run_id, user)
    if (file.content_type or "") not in ("image/png", "image/webp", "image/jpeg"):
        raise HTTPException(400, "Only PNG, WebP or JPEG uploads are supported.")
    data = await file.read()
    if len(data) > 10 * 1024 * 1024:
        raise HTTPException(400, "Image too large (max 10 MB).")
    # Normalize to PNG so the artifact store serves one consistent format.
    from io import BytesIO

    from PIL import Image, UnidentifiedImageError

    try:
        img = Image.open(BytesIO(data))
        buf = BytesIO()
        img.convert("RGBA").save(buf, format="PNG")
        data = buf.getvalue()
    except UnidentifiedImageError as exc:
        raise HTTPException(400, "That file doesn't look like a valid image.") from exc
    token = hashlib.sha256(data).hexdigest()[:16]
    stage, variant = (1, "background") if role == "background" else (2, "subject")
    rel = save_artifact(run["id"], stage, variant, token, data)
    return {"ref": rel, "role": role}


class TweakBody(BaseModel):
    instruction: str


@router.post("/gd/runs/{run_id}/tweak")
def tweak_endpoint(run_id: str, body: TweakBody, user: dict = Depends(get_current_user)) -> dict:
    """Step 5: guardrailed retouch of the approved final (spec 2026-07-15).

    Optional and user-initiated. A rejected tweak stores nothing and surfaces
    the guardrail violations; approving the returned attempt makes it the new
    final via the existing stage-4 approve (which re-archives it)."""
    instruction = (body.instruction or "").strip()
    if not 3 <= len(instruction) <= 500:
        raise HTTPException(400, "Describe the change in 3–500 characters.")
    run = _owned_run(run_id, user)
    attempt = _guard(lambda: pipeline.generate_tweak(run, instruction))
    _log_usage(user, "generate", brand=run.get("brand_id"))
    _advance_run(run, stage=4, stage_status="generated", attempt=attempt)
    return {"attempt": {**attempt, "url": _artifact_url(run_id, attempt["artifact"])},
            "run": _to_client(run)}


class ApproveBody(BaseModel):
    stage: int = Field(ge=1, le=4)
    attempt: int | None = None


@router.post("/gd/runs/{run_id}/approve")
def approve_endpoint(run_id: str, body: ApproveBody, user: dict = Depends(get_current_user)) -> dict:
    run = _owned_run(run_id, user)
    _guard(lambda: pipeline.approve(run, body.stage, body.attempt))
    # Mark this stage approved; approving Stage 4 (the logo composite) completes
    # the whole run.
    _advance_run(
        run,
        stage=body.stage,
        stage_status="approved",
        run_status="completed" if body.stage == 4 else None,
    )
    # A completed run's final creative is archived into the admin Image Library.
    if body.stage == 4:
        _archive_final_image(user, run)
    return _to_client(run)


class BackBody(BaseModel):
    stage: int = Field(ge=1, le=4)


@router.post("/gd/runs/{run_id}/back")
def back_endpoint(run_id: str, body: BackBody, user: dict = Depends(get_current_user)) -> dict:
    run = _owned_run(run_id, user)
    _guard(lambda: pipeline.go_back(run, body.stage))
    return _to_client(run)


# ── prompt audit (build without generating) ───────────────────────────────────
@router.get("/gd/runs/{run_id}/prompt")
def prompt_preview(run_id: str, stage: int, variant: str = "A",
                   user: dict = Depends(get_current_user)) -> dict:
    run = _owned_run(run_id, user)
    if stage == 3:
        variant = "T"
    return _guard(lambda: pipeline.build_prompt(run, stage, variant.upper() if stage in (1, 2) else variant))


# ── suggestions (approval-gated) ──────────────────────────────────────────────
class SuggestBody(BaseModel):
    kind: str  # chat | discovery | concept | explore | gradient | element | aspect_ratio | hooks | font | qa
    answers: dict | None = None
    # Pre-generation discovery brief; merged with the run's persisted brief and the
    # legacy ``answers`` so every suggestion is grounded in the conversation.
    brief: dict | None = None
    # Strategist conversation transcript so far (list of {role:'agent'|'user',text})
    # for kind=="chat". The agent is stateless — the full history is replayed.
    history: list | None = None
    placement: str | None = None
    concept: str | None = None
    stage: int | None = None
    exclude: list[str] | None = None  # variant ids / gradient cids to skip
    steer: str | None = None  # optional free-text nudge for 'gradient'


@router.post("/gd/runs/{run_id}/suggest")
def suggest_endpoint(run_id: str, body: SuggestBody, user: dict = Depends(get_current_user)) -> dict:
    run = _owned_run(run_id, user)
    pack = _pack_for_run(run)
    # The effective brief: the run's persisted discovery answers, overlaid with any
    # brief/answers sent on this call (request wins). Passed as ``answers`` so the
    # existing curated heuristics (which read goal/audience/angle) keep working.
    brief = {**(run["config"].get("creative_brief") or {}), **(body.brief or {}), **(body.answers or {})}
    if body.kind == "chat":
        return suggestions.converse(body.history, brief, pack=pack)
    if body.kind == "discovery":
        return suggestions.synthesize_direction(brief, pack=pack)
    if body.kind == "concept":
        return suggestions.recommend_concept(brief, pack=pack)
    if body.kind == "explore":
        return suggestions.explore_elements(brief, exclude=body.exclude, pack=pack)
    if body.kind == "gradient":
        return suggestions.suggest_gradient(
            brief, steer=body.steer, exclude=body.exclude, pack=pack
        )
    if body.kind == "element":
        return suggestions.suggest_element(
            brief, steer=body.steer, exclude=body.exclude, pack=pack
        )
    if body.kind == "aspect_ratio":
        return suggestions.recommend_aspect_ratio(body.placement)
    if body.kind == "hooks":
        return suggestions.generate_hooks(body.concept, pack=pack)
    if body.kind == "font":
        return suggestions.recommend_font(body.concept, pack=pack)
    if body.kind == "qa":
        return suggestions.qa_critique(body.stage or 1, pack=pack)
    raise HTTPException(400, f"Unknown suggestion kind '{body.kind}'")


class PlanBody(BaseModel):
    brief: str


@router.post("/gd/runs/{run_id}/plan")
def plan_endpoint(run_id: str, body: PlanBody, user: dict = Depends(get_current_user)) -> dict:
    """Auto mode: plan gradient / element / words / logo from the brief.

    Every pick is validated against the run's real pack inventory (retried with
    the errors echoed back); failure is an honest 502, never a fabricated plan.
    The plan is persisted on the run and the brief grounds downstream
    suggestions exactly like a setup-screen brief."""
    from graphics_designer_agent import planner

    run = _owned_run(run_id, user)
    pack = _pack_for_run(run)
    logo_ids = [l["id"] for l in _list_brand_logos(pack)]
    try:
        plan = planner.build_plan(run, pack, body.brief, logo_ids)
    except planner.PlanError as exc:
        raise HTTPException(502, str(exc)) from exc
    run["plan"] = plan
    cfg_brief = dict(run["config"].get("creative_brief") or {})
    cfg_brief["goal"] = plan["brief"]
    run["config"]["creative_brief"] = cfg_brief
    save_run(run)
    _log_usage(user, "plan", brand=run.get("brand_id"))
    return {"plan": plan, "run": _to_client(run)}


# ── artifact streaming ────────────────────────────────────────────────────────
@router.get("/gd/runs/{run_id}/artifact/{rel:path}")
def get_artifact(run_id: str, rel: str, user: dict = Depends(get_current_user)):
    _owned_run(run_id, user)
    from graphics_designer_agent.runs import artifact_abspath, read_artifact

    # Cloud artifacts (gs:// refs) are normally served as signed URLs; this branch
    # is the defensive fallback when signing failed at serialization time.
    if rel.startswith("gs://"):
        try:
            return Response(content=read_artifact(run_id, rel), media_type="image/png")
        except Exception:  # noqa: BLE001
            raise HTTPException(404, "Artifact not found")

    try:
        path = artifact_abspath(run_id, rel)
    except ValueError:
        raise HTTPException(400, "Invalid path")
    if not path.exists():
        raise HTTPException(404, "Artifact not found")
    return Response(content=path.read_bytes(), media_type="image/png")
