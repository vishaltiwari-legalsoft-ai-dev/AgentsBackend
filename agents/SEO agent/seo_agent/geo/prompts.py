"""Per-brand GEO question sets. Configured questions win; otherwise defaults
are templated from the brand's name and category."""

from __future__ import annotations

_DEFAULT_TEMPLATES = (
    "What are the best {category} companies?",
    "Who is {name} and what do they do?",
    "Is {name} a good {category} provider?",
    "What should I look for when choosing a {category} service?",
    "Which {category} providers do experts recommend?",
)


def question_set(brand_cfg: dict) -> list[str]:
    configured = [q for q in brand_cfg.get("questions", []) if q]
    if configured:
        return configured
    name = brand_cfg.get("name", "")
    category = brand_cfg.get("category", "this industry")
    return [t.format(name=name, category=category) for t in _DEFAULT_TEMPLATES]
