"""Stage-3 UI style options: text/CTA placement, colour palette, size bounds.

These drive the per-element control bars in the studio editor. ``phrase`` values
are what reach a prompt (audit summaries); rendering itself is deterministic
(``text_overlay``), so sizes and positions are EXACT.
"""

from __future__ import annotations

# ── Stage 3 — text & CTA placement (§6.4) ─────────────────────────────────────
# The user picks where the text block and the CTA button sit. Each option maps to
# a descriptive phrase substituted into the Stage-3 prompt's placement tokens.
# Defaults reproduce the original left-aligned layout.
TEXT_PLACEMENTS = [
    {"key": "left", "label": "Left",
     "phrase": "the LEFT side of the image — a column occupying roughly the left 40% of the width, vertically centered, leaving the rest of the image clear"},
    {"key": "right", "label": "Right",
     "phrase": "the RIGHT side of the image — a column occupying roughly the right 40% of the width, vertically centered, leaving the rest of the image clear"},
    {"key": "center", "label": "Center",
     "phrase": "the CENTER of the image — a centered column roughly 50–60% wide, balanced vertically, with the underlying subject still visible around it"},
    {"key": "top", "label": "Top",
     "phrase": "a band across the TOP of the image — spanning the upper ~40% of the height, leaving the lower portion clear"},
    {"key": "bottom", "label": "Bottom",
     "phrase": "a band across the BOTTOM of the image — spanning the lower ~40% of the height, leaving the upper portion clear"},
]

CTA_PLACEMENTS = [
    {"key": "bottom", "label": "Below text",
     "phrase": "directly below the sub-text with generous spacing, anchored at the bottom of the text block"},
    {"key": "left", "label": "Left",
     "phrase": "aligned to the LEFT, directly below the sub-text"},
    {"key": "center", "label": "Center",
     "phrase": "CENTERED horizontally, directly below the sub-text"},
    {"key": "right", "label": "Right",
     "phrase": "aligned to the RIGHT, directly below the sub-text"},
    {"key": "top", "label": "Above text",
     "phrase": "ABOVE the headline as a small pill, with the headline and sub-text beneath it"},
]

_TEXT_PLACEMENT_PHRASE = {p["key"]: p["phrase"] for p in TEXT_PLACEMENTS}
_CTA_PLACEMENT_PHRASE = {p["key"]: p["phrase"] for p in CTA_PLACEMENTS}


def text_placement_phrase(key: str) -> str:
    return _TEXT_PLACEMENT_PHRASE.get(key, _TEXT_PLACEMENT_PHRASE["left"])


def cta_placement_phrase(key: str) -> str:
    return _CTA_PLACEMENT_PHRASE.get(key, _CTA_PLACEMENT_PHRASE["bottom"])


# ── Stage 3 — per-element text colour palette (§6.4) ──────────────────────────
# The brand text-gradient stays LOCKED; white is added so a creative on a dark
# photo stays readable. Each text element picks one of these; the CTA button
# keeps its locked orange gradient and is not colour-selectable. ``swatch`` is a
# CSS value the UI renders in the picker; ``phrase`` is what reaches the prompt.
TEXT_COLORS = [
    {"key": "dark", "label": "Dark", "swatch": "#0F0F0F",
     "phrase": "solid near-black #0F0F0F"},
    {"key": "gradient", "label": "Brand gradient",
     "swatch": "linear-gradient(90deg, #86AFFE, #2653AB)",
     "phrase": "a smooth left-to-right linear gradient from #86AFFE to #2653AB applied across the glyphs"},
    {"key": "white", "label": "White", "swatch": "#FFFFFF",
     "phrase": "solid white #FFFFFF"},
]
_TEXT_COLOR_PHRASE = {c["key"]: c["phrase"] for c in TEXT_COLORS}
TEXT_COLOR_KEYS = [c["key"] for c in TEXT_COLORS]


def text_color_phrase(key: str) -> str:
    return _TEXT_COLOR_PHRASE.get(key, _TEXT_COLOR_PHRASE["dark"])


# Styleable Stage-3 elements (drives the per-element control bars in the UI).
# ``placeable`` elements get a placement bar; ``colorable`` ones get the colour
# palette. The highlight is inline in the headline (no placement); the CTA keeps
# its locked orange button (no colour picker).
# The fixed Stage-3 elements that get their own control bar. Sub-headings are a
# DYNAMIC list (1–5) handled separately, so they are no longer listed here. The
# highlight is rendered inline inside the headline, so it follows the headline's
# size + placement (font + colour only). ``sizable`` elements get a size slider.
STAGE3_ELEMENTS = [
    {"key": "headline",  "label": "Heading",         "token": "headline",  "placeable": True,  "colorable": True,  "sizable": True,  "placement_kind": "text"},
    {"key": "highlight", "label": "Hook / highlight", "token": "highlight", "placeable": False, "colorable": True,  "sizable": False, "placement_kind": "text"},
    {"key": "cta",       "label": "CTA button",      "token": "cta",       "placeable": True,  "colorable": True,  "sizable": True,  "placement_kind": "cta"},
]

# ── Stage 3 — deterministic renderer bounds (size slider + pixel nudge) ────────
# Text is rendered deterministically (text_overlay.py) using the real Causten
# fonts, so size and position are EXACT. Size is a percentage of the canvas WIDTH;
# the defaults reproduce the previous prompt's ratios (sub-text ≈ 32% of headline,
# CTA ≈ 38%). Offsets are fine pixel nudges applied after placement anchoring.
TEXT_SIZE_PCT_MIN = 1.5
TEXT_SIZE_PCT_MAX = 18.0
DEFAULT_TEXT_SIZE_PCT = {"headline": 8.0, "subheading": 3.0, "cta": 3.4}
TEXT_OFFSET_PX_RANGE = 600

# Number of sub-heading lines the user may add (1–5, defaulting to 2).
SUBHEADING_MIN = 1
SUBHEADING_MAX = 5
