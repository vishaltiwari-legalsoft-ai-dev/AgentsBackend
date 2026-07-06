"""Graphics Designer agent (marketing department) — Legal Soft 4-stage ad pipeline.

Importable package root: ``graphics_designer_agent`` (placed on ``sys.path`` by
``app/__init__.py`` because the parent folders contain spaces).

Layout — one folder per pipeline step, shared engine at the root:

- ``stage1_gradient/`` — Stage 1: gradient/background foundation
- ``stage2_element/``  — Stage 2: subject/element blend
- ``stage3_text/``     — Stage 3: deterministic text overlay + element canvas
- ``stage4_logo/``     — Stage 4: logo composite
- root modules         — cross-stage engine: ``pipeline`` (state machine),
  ``runs`` (persistence), ``registry`` (+ ``templated_brands``/``brands/``,
  multi-brand packs), ``providers`` (image models), ``prompts`` (canonical .txt
  loading + integrity), ``suggestions`` (AI strategist), ``tokens``
  (substitution engine + shared constants), ``variants`` (locked brand kit),
  ``reference_library`` (brand precedent retrieval)
- ``creative/``        — multi-frame creative rail (brochure/PPTX/carousel/blog)
  built on the same 4-step backbone
"""

from __future__ import annotations

from . import pipeline, stage1_gradient, stage2_element, stage3_text, stage4_logo
from . import suggestions, tokens, variants
from .prompts import CANONICAL_SHA256, load_prompt, verify_integrity
from .runs import create_run, get_run, save_run

__all__ = [
    "pipeline",
    "stage1_gradient",
    "stage2_element",
    "stage3_text",
    "stage4_logo",
    "suggestions",
    "tokens",
    "variants",
    "CANONICAL_SHA256",
    "load_prompt",
    "verify_integrity",
    "create_run",
    "get_run",
    "save_run",
]
