"""Text Optimizer polish prompts — the three style recipes (spec 2026-07-14).

Every recipe shares one PRESERVATION_BLOCK: the image model may refine and
integrate, but the composite's content is immutable — fonts, text, gradient,
photo and existing elements can never change. The recipes are data, not code:
add or remove a style by editing STYLE_RECIPES.
"""

from __future__ import annotations

PRESERVATION_BLOCK = (
    "NON-NEGOTIABLE PRESERVATION RULES — violating any one ruins the output:\n"
    "1. Keep ALL text character-for-character identical and fully legible. Never\n"
    "   add, remove, reword or re-case a word.\n"
    "2. Never change a letterform: same font family, same weights, same spacing\n"
    "   within each block, same sizes, same colours on every text element.\n"
    "3. Never alter, recolor, redraw or remove ANY element already present\n"
    "   (shapes, icons, stickers, buttons, dividers).\n"
    "4. Never modify the background gradient — colours, direction and stops stay\n"
    "   exactly as they are.\n"
    "5. Never modify the photo/subject — same person or object, same pose, same crop.\n"
    "THE ONE ALLOWED ADJUSTMENT — collision fix: if any text block or the CTA\n"
    "button overlaps the photo/subject or another element, MOVE that whole block\n"
    "(unchanged, as one unit) to the nearest clean negative space so NOTHING\n"
    "overlaps the subject and every word stays fully readable. Reposition only —\n"
    "never resize, restyle or reflow it.\n"
    "Your job is finishing, integration and collision-free placement ONLY."
)

STYLE_RECIPES = [
    {
        "key": "brand_strict",
        "label": "Brand strict",
        "intent": (
            "Integrate the overlaid text with the scene so the creative reads as one "
            "professionally finished brand asset: unify lighting and shadows between "
            "text and background, clean up rough edges around the text, add subtle "
            "believable depth (a soft shadow or gentle contact light) where text meets "
            "the scene. Stay STRICTLY inside the existing brand palette — introduce no "
            "new colours and no new visual elements."
        ),
    },
    {
        "key": "highlighted",
        "label": "Highlighted",
        "intent": (
            "Make the creative stronger and more appealing by ADDING subtle emphasis "
            "around the existing focal points: a gentle glow or accent stroke behind "
            "the highlighted headline words, a soft contrast pop that draws the eye to "
            "the CTA button. Use ONLY colours already present in the image. Everything "
            "you add must be new and additive — never modify what is already there."
        ),
    },
    {
        "key": "sharp_minimal",
        "label": "Sharp minimal",
        "intent": (
            "Finish the creative with high sharpness and a minimal, elegant touch: "
            "crisp edges, refined micro-contrast, calm even tonality, immaculate clean "
            "rendering. Add NO new decoration or effects — the elegance comes from "
            "restraint and finishing quality, not additions. Remove nothing either."
        ),
    },
]

STYLE_KEYS = [r["key"] for r in STYLE_RECIPES]

_H = ("left", "center", "right")
_V = ("top", "middle", "bottom")


def _zone_name(x, y) -> str:
    h = _H[min(2, int(float(x) * 3))]
    v = _V[min(2, int(float(y) * 3))]
    return "center" if (v == "middle" and h == "center") else f"{v} {h}"


def describe_layout(layers: list[dict]) -> str:
    """Human-readable, prompt-ready summary of where every visible layer sits.

    Derived from the ACTUAL resolved coords (``layout.resolve_layers`` output),
    so the polish prompt describes the user's real arrangement — pinned or auto.
    """
    lines: list[str] = []
    for layer in layers:
        kind = layer.get("type")
        zone = _zone_name(layer.get("x", 0.5), layer.get("y", 0.5))
        if kind == "text":
            txt = (layer.get("text") or "").strip()
            if not txt:
                continue
            label = str(layer.get("id", "text")).replace("-", " ").upper()
            lines.append(f'{label} "{txt[:60]}" — {zone}')
        elif kind == "cta":
            txt = (layer.get("text") or "").strip()
            if txt:
                lines.append(f'CTA BUTTON "{txt[:40]}" — {zone}')
        elif kind in ("shape", "element"):
            lines.append(f"{str(layer.get('kind', 'element')).upper()} — {zone}")
    return "\n".join(lines) or "(text overlay as shown)"


def build_polish_prompt(style_key: str, layout_desc: str, notes: str = "") -> str:
    recipe = next(r for r in STYLE_RECIPES if r["key"] == style_key)
    parts = [
        "You are a senior brand designer doing the final polish pass on a "
        "social-media creative. The attached image is the FINISHED composite: brand "
        "background, subject and overlaid text are already in their final arrangement.",
        f"STYLE DIRECTION — {recipe['label']}:\n{recipe['intent']}",
        "CURRENT LAYOUT (keep each element here UNLESS it collides with the "
        "subject — then apply the collision fix below):\n" + layout_desc,
    ]
    if (notes or "").strip():
        parts.append("DESIGNER NOTES (from the user):\n" + notes.strip()[:500])
    parts.append(PRESERVATION_BLOCK)
    parts.append("Return the polished image at the same aspect ratio.")
    return "\n\n".join(parts)
