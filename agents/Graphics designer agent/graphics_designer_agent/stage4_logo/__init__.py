"""Stage 4 — logo composite onto the approved Stage-3 creative.

``compositor`` is the deterministic Pillow compositor (white-box keying,
placement math); ``options`` holds the UI placement grid + slider bounds. The
AI-compositor path's prompt stays in ``../prompts/stage4_logo_composite.txt``.
"""

from __future__ import annotations

from .compositor import composite_logo, default_logo_layout, logo_placement
from .options import (
    LOGO_OFFSET_PX_RANGE,
    LOGO_POSITIONS,
    LOGO_SIZE_PCT_MAX,
    LOGO_SIZE_PCT_MIN,
)

__all__ = [
    "composite_logo",
    "default_logo_layout",
    "logo_placement",
    "LOGO_OFFSET_PX_RANGE",
    "LOGO_POSITIONS",
    "LOGO_SIZE_PCT_MAX",
    "LOGO_SIZE_PCT_MIN",
]
