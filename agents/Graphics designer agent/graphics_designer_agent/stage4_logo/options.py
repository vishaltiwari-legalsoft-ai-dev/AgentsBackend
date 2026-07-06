"""Stage-4 UI options: the logo placement grid + slider bounds.

``key`` matches ``compositor.LOGO_POSITION_KEYS``; row/col drive the 3×3 grid
the studio renders as the logo placement guide.
"""

from __future__ import annotations

LOGO_POSITIONS = [
    {"key": "top-left", "label": "Top left", "row": 0, "col": 0},
    {"key": "top-center", "label": "Top center", "row": 0, "col": 1},
    {"key": "top-right", "label": "Top right", "row": 0, "col": 2},
    {"key": "middle-left", "label": "Middle left", "row": 1, "col": 0},
    {"key": "middle-center", "label": "Center", "row": 1, "col": 1},
    {"key": "middle-right", "label": "Middle right", "row": 1, "col": 2},
    {"key": "bottom-left", "label": "Bottom left", "row": 2, "col": 0},
    {"key": "bottom-center", "label": "Bottom center", "row": 2, "col": 1},
    {"key": "bottom-right", "label": "Bottom right", "row": 2, "col": 2},
]

# Slider bounds for the logo size (% of canvas width) and fine px nudge range.
LOGO_SIZE_PCT_MIN = 4
LOGO_SIZE_PCT_MAX = 60
LOGO_OFFSET_PX_RANGE = 400
