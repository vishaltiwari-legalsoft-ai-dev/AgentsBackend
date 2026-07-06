"""Shared token-substitution engine + cross-stage constants (spec §6).

Substitutions are ordered *exact-string* replacements of a canonical DEFAULT
value with the user/agent-approved value. When a value equals its default the
replacement is a no-op, which is what guarantees the §9.1 byte-identical
property. Because we only ever replace whitelisted default strings, no other
byte can change — that is the §9.2 isolation property, by construction.

The per-stage substitutions live with their stages:
``stage1_gradient.prompting`` / ``stage2_element.prompting`` /
``stage3_text.prompting``. This module owns only the engine (``apply_token``,
``Diff``, ``Substitution``) and the constants shared by more than one stage
(aspect-ratio presets, brand font default, placement defaults).
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The locked brand font default (a Causten variant — see ``variants.FONTS``).
DEFAULT_FONT = "Causten Bold"

DEFAULT_TEXT_PLACEMENT = "left"
DEFAULT_CTA_PLACEMENT = "bottom"


def default_element_styles() -> dict:
    """The factory Stage-3 styling — reproduces the original left-aligned, dark
    headline / gradient-highlight layout. Each element is independently
    overridable by the user from here."""
    return {
        "headline":  {"font": DEFAULT_FONT, "color": "dark",     "placement": DEFAULT_TEXT_PLACEMENT},
        "highlight": {"font": DEFAULT_FONT, "color": "gradient"},
        "subtext1":  {"font": DEFAULT_FONT, "color": "dark",     "placement": DEFAULT_TEXT_PLACEMENT},
        "subtext2":  {"font": DEFAULT_FONT, "color": "dark",     "placement": DEFAULT_TEXT_PLACEMENT},
        "cta":       {"font": DEFAULT_FONT, "placement": DEFAULT_CTA_PLACEMENT},
    }

# Aspect-ratio presets (§6.2). Key is the AR token.
ASPECT_RATIOS: dict[str, dict] = {
    "4:5": {"label": "Instagram Portrait", "w": 1080, "h": 1350, "orientation": "Vertical", "default": True},
    "1:1": {"label": "Square", "w": 1080, "h": 1080, "orientation": "Square", "default": False},
    "9:16": {"label": "Story / Reel", "w": 1080, "h": 1920, "orientation": "Vertical", "default": False},
    "16:9": {"label": "Landscape", "w": 1920, "h": 1080, "orientation": "Horizontal", "default": False},
}
DEFAULT_AR = "4:5"


@dataclass
class Diff:
    token: str
    find: str
    replace: str
    count: int


@dataclass
class Substitution:
    text: str
    diffs: list[Diff] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def apply_token(text: str, token: str, default: str, value: str | None,
                diffs: list[Diff], warnings: list[str]) -> str:
    """Replace every occurrence of ``default`` with ``value`` (no-op if equal)."""
    if value is None or value == default:
        return text
    count = text.count(default)
    if count == 0:
        warnings.append(f"anchor for '{token}' not found — substitution skipped")
        return text
    diffs.append(Diff(token=token, find=default, replace=value, count=count))
    return text.replace(default, value)
