"""Stage 2 — subject/element blend onto the approved Stage-1 background.

``prompting`` builds the blend prompt ([SUBJECT] substitution + the 9-cell
prompt-steered placement); ``variants`` is the curated subject library. The
canonical blend prompt stays in ``../prompts/stage2_element_blend.txt``.
"""

from __future__ import annotations

from .prompting import SUBJECT_ANCHOR, place_subject, substitute_stage2
from .variants import (
    STAGE2_BLEND_PROMPT,
    STAGE2_CATEGORIES,
    STAGE2_PLACEMENTS,
    STAGE2_VARIANTS,
)

__all__ = [
    "SUBJECT_ANCHOR",
    "place_subject",
    "substitute_stage2",
    "STAGE2_BLEND_PROMPT",
    "STAGE2_CATEGORIES",
    "STAGE2_PLACEMENTS",
    "STAGE2_VARIANTS",
]
