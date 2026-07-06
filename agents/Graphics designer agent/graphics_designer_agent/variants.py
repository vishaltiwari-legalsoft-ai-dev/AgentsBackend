"""Shared brand kit: locked colours, the Causten font family, AR preset list.

None of the copy here is ever injected into a prompt (spec §5.2). It exists only
to render the studio UI and to drive the deterministic renderer's brand theme.

Per-stage UI metadata lives with its stage: ``stage1_gradient.variants``
(gradient concept cards), ``stage2_element.variants`` (subject library +
placement grid), ``stage3_text.style_options`` (text placements/colours/sizes)
and ``stage4_logo.options`` (logo grid + slider bounds).
"""

from __future__ import annotations

from .tokens import ASPECT_RATIOS, DEFAULT_FONT

# ── Locked brand system (spec §2.2 / §2.3) — read-only in the UI ──────────────
LOCKED_COLORS = {
    "gradient": ["#FFFFFF", "#BDCFED", "#A2C0E6", "#1746A2"],
    "text": "#0F0F0F",
    "accent": "#85AEFD",
    "headline_highlight": {"from": "#86AFFE", "to": "#2653AB", "direction": "left-to-right linear"},
    "cta": {
        "from": "#FF8A3D",
        "to": "#F26A1A",
        "direction": "135° diagonal",
        "shadow": "rgba(242, 106, 26, 0.25) 0 8px 20px",
    },
}

# Authored verbatim (spec §2.3) — display exactly as-is.
BRAND_KIT_BLOCK = (
    "{\n"
    "Vertical gradient right side : #BDCFED TO #1746A2\n"
    "Vertical gradient left side : #FFFFFF TO #1746A2\n"
    "Horizontal gradient top left to top right : #FFFFFF TO #A2C0E6\n"
    "Horizontal gradient bottom left to bottom right : #1746A2 TO #1746A2\n"
    "}"
)

# ── Fonts (spec §6.1) ─────────────────────────────────────────────────────────
# The creative font is LOCKED to a single brand family: Causten. Users may pick
# any of its variations (weight + upright/oblique), but never a different family.
# The .otf files live in ``<agent>/Causten Font Family`` and are the canonical
# reference for what "Causten <variant>" means on a creative.
FONT_FAMILY = "Causten"

# Ordered Thin → Black; each weight in upright then oblique. ``file`` is the
# matching face under the Causten Font Family folder.
FONT_VARIANTS = [
    {"name": "Causten Thin", "weight": 100, "style": "normal", "file": "Causten-Thin.otf"},
    {"name": "Causten Thin Oblique", "weight": 100, "style": "oblique", "file": "Causten-ThinOblique.otf"},
    {"name": "Causten ExtraLight", "weight": 200, "style": "normal", "file": "Causten-ExtraLight.otf"},
    {"name": "Causten ExtraLight Oblique", "weight": 200, "style": "oblique", "file": "Causten-ExtraLightOblique.otf"},
    {"name": "Causten Light", "weight": 300, "style": "normal", "file": "Causten-Light.otf"},
    {"name": "Causten Light Oblique", "weight": 300, "style": "oblique", "file": "Causten-LightOblique.otf"},
    {"name": "Causten Regular", "weight": 400, "style": "normal", "file": "Causten-Regular.otf"},
    {"name": "Causten Regular Oblique", "weight": 400, "style": "oblique", "file": "Causten-RegularOblique.otf"},
    {"name": "Causten Medium", "weight": 500, "style": "normal", "file": "Causten-Medium.otf"},
    {"name": "Causten Medium Oblique", "weight": 500, "style": "oblique", "file": "Causten-MediumOblique.otf"},
    {"name": "Causten SemiBold", "weight": 600, "style": "normal", "file": "Causten-SemiBold.otf"},
    {"name": "Causten SemiBold Oblique", "weight": 600, "style": "oblique", "file": "Causten-SemiBoldOblique.otf"},
    {"name": "Causten Bold", "weight": 700, "style": "normal", "file": "Causten-Bold.otf"},
    {"name": "Causten Bold Oblique", "weight": 700, "style": "oblique", "file": "Causten-BoldOblique.otf"},
    {"name": "Causten ExtraBold", "weight": 800, "style": "normal", "file": "Causten-ExtraBold.otf"},
    {"name": "Causten ExtraBold Oblique", "weight": 800, "style": "oblique", "file": "Causten-ExtraBoldOblique.otf"},
    {"name": "Causten Black", "weight": 900, "style": "normal", "file": "Causten-Black.otf"},
    {"name": "Causten Black Oblique", "weight": 900, "style": "oblique", "file": "Causten-BlackOblique.otf"},
]

# Flat list of selectable names (Thin → Black), consumed by the UI dropdown. The
# default selection is carried by the run config, not by list position.
FONTS = [v["name"] for v in FONT_VARIANTS]
assert DEFAULT_FONT in FONTS, "DEFAULT_FONT must be one of the Causten variants"

_FONT_FILE = {v["name"]: v["file"] for v in FONT_VARIANTS}


def font_file(name: str) -> str:
    """The brand .otf/.ttf filename for a selectable font name (falls back to the
    default weight if an unknown name slips through). Used by the Stage-3 renderer
    for the default (Legal Soft) theme; each brand pack supplies its own resolver."""
    return _FONT_FILE.get(name, _FONT_FILE[DEFAULT_FONT])


# ── Aspect-ratio presets for the UI dropdown (spec §6.2) ──────────────────────
ASPECT_RATIO_PRESETS = [
    {
        "ar": ar,
        "label": meta["label"],
        "dimensions": f"{meta['w']}x{meta['h']}px",
        "w": meta["w"],
        "h": meta["h"],
        "orientation": meta["orientation"],
        "default": meta["default"],
    }
    for ar, meta in ASPECT_RATIOS.items()
]
