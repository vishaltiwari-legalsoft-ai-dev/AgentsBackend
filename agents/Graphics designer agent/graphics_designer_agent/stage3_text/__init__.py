"""Stage 3 — deterministic text overlay (no image model).

Everything the third step owns lives here:

- ``text_overlay`` — the Pillow renderer (fonts, wrapping, CTA pill, gradients)
- ``layout`` — run config → resolved layer list (auto-vs-pinned model)
- ``render`` / ``render_contract`` — engine dispatch (Pillow vs Konva service)
- ``elements`` — the Canva-style element library (emoji/icons/uploads)
- ``shapes`` / ``icons`` — 2D shapes + infographic glyphs
- ``prompting`` — content tokens + audit-prompt substitution / validation
- ``style_options`` — UI options: placements, colours, size/offset bounds

The canonical prompt stays in ``../prompts/stage3_text_overlay.txt``.
"""

from __future__ import annotations

from .prompting import (
    DEFAULT_CTA,
    DEFAULT_HEADLINE,
    DEFAULT_HIGHLIGHT,
    DEFAULT_SUBTEXT_1,
    DEFAULT_SUBTEXT_2,
    STAGE3_STYLE_ANCHORS,
    substitute_stage3,
    validate_stage3_tokens,
)
from .style_options import (
    CTA_PLACEMENTS,
    DEFAULT_TEXT_SIZE_PCT,
    STAGE3_ELEMENTS,
    SUBHEADING_MAX,
    SUBHEADING_MIN,
    TEXT_COLOR_KEYS,
    TEXT_COLORS,
    TEXT_OFFSET_PX_RANGE,
    TEXT_PLACEMENTS,
    TEXT_SIZE_PCT_MAX,
    TEXT_SIZE_PCT_MIN,
    cta_placement_phrase,
    text_color_phrase,
    text_placement_phrase,
)

__all__ = [
    "DEFAULT_CTA",
    "DEFAULT_HEADLINE",
    "DEFAULT_HIGHLIGHT",
    "DEFAULT_SUBTEXT_1",
    "DEFAULT_SUBTEXT_2",
    "STAGE3_STYLE_ANCHORS",
    "substitute_stage3",
    "validate_stage3_tokens",
    "CTA_PLACEMENTS",
    "DEFAULT_TEXT_SIZE_PCT",
    "STAGE3_ELEMENTS",
    "SUBHEADING_MAX",
    "SUBHEADING_MIN",
    "TEXT_COLOR_KEYS",
    "TEXT_COLORS",
    "TEXT_OFFSET_PX_RANGE",
    "TEXT_PLACEMENTS",
    "TEXT_SIZE_PCT_MAX",
    "TEXT_SIZE_PCT_MIN",
    "cta_placement_phrase",
    "text_color_phrase",
    "text_placement_phrase",
]
