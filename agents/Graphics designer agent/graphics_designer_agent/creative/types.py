"""Creative-Agent taxonomy: the 4-step model, routing, and the autonomous warning.

This sits one layer above ``reference_library.CREATIVE_TYPES`` (the format/routing
source of truth). It adds the things the *agent* needs that the library does not:
the four pipeline steps, the per-type planning hint, and the mandatory warning the
UI must show — and the user must acknowledge — before autonomous mode takes over.
"""

from __future__ import annotations

from typing import Any

from .. import reference_library as rl

# The four steps every Creative-Agent run moves through (spec "Autonomous Mode").
# In manual mode the user drives each; in autonomous mode the agent runs them all,
# logging a decision at each step.
STEPS: list[dict[str, str]] = [
    {"key": "intent", "label": "User intent gathering",
     "detail": "Understand the goal, audience, and message for this creative."},
    {"key": "strategy", "label": "Creative strategy & concept",
     "detail": "Decide the angle, tone, and the structure of the piece."},
    {"key": "layout", "label": "Layout, design & element placement",
     "detail": "Plan frames/slides/sections, copy, and visual hierarchy."},
    {"key": "output", "label": "Asset selection & final output",
     "detail": "Render the finished PDF / PPTX / image set, grounded in brand precedent."},
]

STEP_KEYS = [s["key"] for s in STEPS]


def step_index(key: str) -> int:
    return STEP_KEYS.index(key) if key in STEP_KEYS else -1


# Verbatim from the spec — shown on autonomous activation; the user must
# acknowledge it before the agent proceeds.
AUTONOMOUS_WARNING = (
    "Autonomous Mode is now active. The agent will handle your entire creative "
    "end-to-end based on AI recommendations. You can review and override at any "
    "point, but all decisions — including layout, copy, colors, and assets — will "
    "be made by the agent until you manually take control."
)

# Per-type guidance the planner leans on. Frame/section counts are *defaults* the
# agent may adjust for the brief.
PLAN_HINTS: dict[str, dict[str, Any]] = {
    "carousel": {"unit": "frame", "default_count": 5, "min": 3, "max": 8,
                 "roles": ["hook", "body", "cta"]},
    "presentation": {"unit": "slide", "default_count": 6, "min": 3, "max": 15,
                     "roles": ["title", "content", "closing"]},
    "brochure": {"unit": "section", "default_count": 4, "min": 2, "max": 8,
                 "roles": ["cover", "content", "contact"]},
    "blog": {"unit": "image", "default_count": 3, "min": 1, "max": 6,
             "roles": ["cover", "inline"]},
}


def creative_agent_types() -> list[dict[str, Any]]:
    """The types this agent owns (everything that routes here), UI-ready."""
    out: list[dict[str, Any]] = []
    for key, spec in rl.CREATIVE_TYPES.items():
        if spec.get("routes_to") != "creative_agent":
            continue
        hint = PLAN_HINTS.get(key, {})
        out.append({
            "key": key,
            "label": spec.get("label", key),
            "aspect_ratio": spec.get("aspect_ratio"),
            "orientation": spec.get("orientation"),
            "multi_frame": spec.get("multi_frame", False),
            "output_format": spec.get("output_format", "image"),
            "notes": spec.get("notes", ""),
            "unit": hint.get("unit", "frame"),
            "default_count": hint.get("default_count", 1),
            "min_count": hint.get("min", 1),
            "max_count": hint.get("max", 12),
        })
    return out


def is_creative_agent_type(creative_type: str) -> bool:
    return rl.routes_to_creative_agent(creative_type)


def require_known(creative_type: str) -> None:
    if not rl.is_known_type(creative_type):
        raise ValueError(f"Unknown creative type: {creative_type}")
    if not rl.routes_to_creative_agent(creative_type):
        raise ValueError(
            f"'{creative_type}' is a standard social post — use the Graphics "
            f"Studio editor, not the Creative Agent."
        )
