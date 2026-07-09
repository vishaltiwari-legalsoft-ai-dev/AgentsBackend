# backend/app/services/gd_spec_builder.py
"""Convert an extracted brand-kit profile (`BrandKitProfile`) plus its
`BrandFolder` into the spec dict contract that
`graphics_designer_agent.templated_brands.build_templated_pack` consumes.

Pure module: no Firestore/GCS/HTTP — the enrichment orchestrator
(brand_enrichment.py) owns wiring the result into `patch["gd_spec"]`.
"""
from __future__ import annotations

import json
import re
from collections import Counter
from typing import Callable

from app.services.brand_folder_scanner import BrandFolder
from app.services.brand_kit_extractor import BrandKitProfile, fonts_from_files

_SLUG_SEP_RE = re.compile(r"[ _]+")
_SLUG_INVALID_RE = re.compile(r"[^a-z0-9-]")
_SLUG_DASH_RE = re.compile(r"-+")


def _slug(name: str) -> str:
    """lowercase; spaces/underscores -> '-'; strip every char not
    [a-z0-9-]; collapse repeat dashes; trim leading/trailing dashes."""
    s = name.lower()
    s = _SLUG_SEP_RE.sub("-", s)
    s = _SLUG_INVALID_RE.sub("", s)
    s = _SLUG_DASH_RE.sub("-", s)
    return s.strip("-")


# Six industry-neutral Stage-2 subject variants (A-F), mirroring the
# id/title/angle/category/desc/subject structure of
# templated_brands._MEDVIRTUAL["stage2_variants"]. This prose is fed
# verbatim to an image-generation model downstream, so the frame-placement
# language ("occupying the right side, left kept open") is load-bearing.
GENERIC_STAGE2_VARIANTS = [
    {
        "id": "A", "title": "Professional at Desk", "angle": "efficiency / warmth",
        "category": "people",
        "desc": "Warm professional with a headset; lower frame, upper area open.",
        "subject": (
            "A warm, confident professional in smart business-casual attire wearing a "
            "slim wireless headset, seated at a tidy modern desk and looking slightly "
            "off-camera as if mid-conversation, cinematic shallow depth of field. They "
            "occupy the lower portion of the frame; keep the upper area open for headline text."
        ),
    },
    {
        "id": "B", "title": "Composed Team Lead", "angle": "authority / trust",
        "category": "people",
        "desc": "Composed team lead on the right; left kept open.",
        "subject": (
            "A composed, confident team lead in tailored business-casual attire with a "
            "slim wireless headset, calm assured posture and a slight forward lean, "
            "occupying the right side of the frame and leaving the left open for copy. "
            "Soft studio-quality lighting, premium editorial feel."
        ),
    },
    {
        "id": "C", "title": "Minimal Icon Object", "angle": "simplicity / precision",
        "category": "object",
        "desc": "Ultra-minimal 3D object; centered-low, open frame.",
        "subject": (
            "An ultra-minimal matte 3D rendering of a single modern laptop, angled "
            "three-quarters open with a soft glow from the screen, resting with one "
            "gentle shadow beneath it, centered-low with vast empty space around it. "
            "Restrained, premium, no clutter, no text on screen."
        ),
    },
    {
        "id": "D", "title": "Organised Desk Flatlay", "angle": "organised productivity",
        "category": "flatlay",
        "desc": "Top-down tidy desk; cluster lower-right, upper-left clear.",
        "subject": (
            "A top-down flatlay of a tidy modern workspace: an open laptop, a notebook "
            "with a pen resting across it, a pair of folded glasses and a small white "
            "coffee cup, arranged in a loose cluster in the lower-right with the "
            "upper-left kept clear. Soft overhead daylight, faint natural shadows, clean "
            "isolated objects, no visible text or logos."
        ),
    },
    {
        "id": "E", "title": "Modern Office Tower", "angle": "scale / trust",
        "category": "architecture",
        "desc": "Clean modern building on the right; left kept open.",
        "subject": (
            "A modern office building with a clean glass-and-steel facade, shot from a "
            "gentle upward angle at golden hour with warm rim light, occupying the right "
            "side of the frame and leaving the left open for headline copy. No people, "
            "no visible signage or text."
        ),
    },
    {
        "id": "F", "title": "Quiet Workspace, After Hours", "angle": "pain-point / relief",
        "category": "scene",
        "desc": "Empty calm workspace in the evening; right side, ample negative space.",
        "subject": (
            "A quiet, empty modern office space after hours — a tidy desk, a vacant "
            "ergonomic chair and soft ambient lighting, no people — evoking a calm "
            "end-of-day moment, set into the right side of the frame with ample negative "
            "space on the left for headline and CTA."
        ),
    },
]

# Generic, brand-name-parameterized default content.
_DEFAULT_SUBTEXT_1 = "Expert support, ready when you are."
_DEFAULT_SUBTEXT_2 = "Onboard trusted talent in days, not months."
_DEFAULT_CTA = "Book a Free Consultation"
_DEFAULT_CTAS = ["Book a Free Consultation", "Get Started Today", "Talk to Our Team"]


def _font_variants_from_folder(folder: BrandFolder) -> tuple[list[dict], str, str, bool]:
    """Returns (font_variants, font_family, default_font, font_fallback).

    Non-empty ``folder.font_files`` -> derive via the extractor's
    ``fonts_from_files`` filename parsing (never reimplemented here).
    Empty -> fall back to the Be Vietnam set, imported verbatim.
    """
    if not folder.font_files:
        from graphics_designer_agent.templated_brands import _BEVIETNAM_FULL
        return list(_BEVIETNAM_FULL), "Be Vietnam", "Be Vietnam Bold", True

    hits = fonts_from_files(folder.font_files)
    variants = [{"name": f"{h.family} {h.style}", "file": h.raw_name} for h in hits]
    font_family = Counter(h.family for h in hits).most_common(1)[0][0]
    default_hit = next((h for h in hits if "Bold" in h.style), hits[0])
    default_font = f"{default_hit.family} {default_hit.style}"
    return variants, font_family, default_font, False


def _synthesize_content(profile: BrandKitProfile,
                        llm: Callable[[str], str]) -> dict | None:
    """One JSON-guarded LLM call for brand-tuned copy (same pattern as
    brand_kit_extractor._label_with_llm): accepted only if parseable AND
    every value is a non-empty string / non-empty list of non-empty
    strings. Any failure -> None (caller keeps generics)."""
    font_names = sorted({f"{f.family} {f.style}" for f in profile.fonts})
    prompt = (
        f"You are writing ad copy for the brand '{profile.brand_name}'. "
        f"Tone of voice: {profile.tone_of_voice or 'not specified'}. "
        f"Brand fonts: {font_names or 'not specified'}. "
        'Reply with JSON only: {"default_headline": "", "default_highlight": "", '
        '"default_subtext_1": "", "default_subtext_2": "", "ctas": [""]}'
    )
    try:
        raw = llm(prompt)
        data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
    except Exception:
        return None

    str_keys = ("default_headline", "default_highlight",
               "default_subtext_1", "default_subtext_2")
    for key in str_keys:
        val = data.get(key)
        if not isinstance(val, str) or not val.strip():
            return None

    ctas = data.get("ctas")
    if (not isinstance(ctas, list) or not ctas
            or not all(isinstance(c, str) and c.strip() for c in ctas)):
        return None

    return {**{k: data[k] for k in str_keys}, "ctas": ctas}


def build_gd_spec(profile: BrandKitProfile, folder: BrandFolder,
                  brand_id: str | None,
                  llm: Callable[[str], str] | None = None) -> dict | None:
    """Assemble the `build_templated_pack` spec contract from a brand-kit
    profile. Returns None when the profile has fewer than 3 extracted
    colors (`profile.palette` is empty) — the brand isn't generatable yet."""
    if not profile.palette:
        return None

    font_variants, font_family, default_font, font_fallback = \
        _font_variants_from_folder(folder)

    content = {
        "default_headline": f"Grow Faster With {profile.brand_name}",
        "default_highlight": profile.brand_name,
        "default_subtext_1": _DEFAULT_SUBTEXT_1,
        "default_subtext_2": _DEFAULT_SUBTEXT_2,
        "ctas": list(_DEFAULT_CTAS),
    }
    if llm is not None:
        synthesized = _synthesize_content(profile, llm)
        if synthesized is not None:
            # highlight must be a substring of headline — otherwise revert
            # just those two fields to the generic pair; the rest of the
            # synthesized content (subtext/ctas) still independently
            # satisfied the JSON-guard shape check and stays applied.
            if synthesized["default_highlight"] in synthesized["default_headline"]:
                content["default_headline"] = synthesized["default_headline"]
                content["default_highlight"] = synthesized["default_highlight"]
            content["default_subtext_1"] = synthesized["default_subtext_1"]
            content["default_subtext_2"] = synthesized["default_subtext_2"]
            content["ctas"] = synthesized["ctas"]

    return {
        "id": _slug(profile.brand_name),
        "name": profile.brand_name,
        "firestore_brand_id": brand_id,
        "palette": dict(profile.palette),
        "font_family": font_family,
        "font_variants": font_variants,
        "default_font": default_font,
        "font_fallback": font_fallback,
        "default_headline": content["default_headline"],
        "default_highlight": content["default_highlight"],
        "default_subtext_1": content["default_subtext_1"],
        "default_subtext_2": content["default_subtext_2"],
        "default_cta": _DEFAULT_CTA,
        "ctas": content["ctas"],
        "hooks": {},
        "stage2_variants": list(GENERIC_STAGE2_VARIANTS),
    }
