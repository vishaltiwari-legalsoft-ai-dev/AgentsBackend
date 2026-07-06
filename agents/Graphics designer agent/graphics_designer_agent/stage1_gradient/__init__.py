"""Stage 1 — gradient/background foundation.

Everything specific to the pipeline's first step lives here:
``prompting`` (the Stage-1 prompt substitution) and ``variants`` (the curated
gradient concept cards). The canonical prompt .txt files stay in
``../prompts/stage1_gradient_*.txt`` (their bytes are integrity-locked).
"""

from __future__ import annotations

from .prompting import STAGE1_AR_ANCHOR, substitute_stage1
from .variants import SOURCE_NOTE_STAGE1, STAGE1_VARIANTS

__all__ = [
    "STAGE1_AR_ANCHOR",
    "substitute_stage1",
    "SOURCE_NOTE_STAGE1",
    "STAGE1_VARIANTS",
]
