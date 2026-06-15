"""Graphics Designer agent (marketing department) — Legal Soft 4-stage ad pipeline.

Importable package root: ``graphics_designer_agent`` (placed on ``sys.path`` by
``app/__init__.py`` because the parent folders contain spaces). See ``agent.md``
for the full build spec and ``README.md`` for the layout.
"""

from __future__ import annotations

from . import pipeline, suggestions, tokens, variants
from .prompts import CANONICAL_SHA256, load_prompt, verify_integrity
from .runs import create_run, get_run, save_run

__all__ = [
    "pipeline",
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
