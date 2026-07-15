"""Step 5 — optional final tweak (spec 2026-07-15).

The user's minor change request + the APPROVED Stage-4 final go to the premium
image model behind absolute guardrails. The result is QA-gated
(``qa_brain.check_tweak``): one retry with violations fed back, then an HONEST
rejection — the approved final is never replaced by a violating image, and a
tweak is never faked.
"""

from __future__ import annotations

import logging

from .stage3_text import qa_brain

logger = logging.getLogger("graphics_designer.final_tweak")

_MAX_TWEAK_ATTEMPTS = 2  # first try + one retry with violations fed back

TWEAK_GUARDRAILS = (
    "ABSOLUTE GUARDRAILS — these override the requested change itself:\n"
    "1. Never change the font family or any font: every letterform, weight,\n"
    "   spacing and text colour stays exactly as it is.\n"
    "2. Never change or manipulate the LOGO in any way — do not move, redraw,\n"
    "   recolor, resize, restyle, sharpen or 'improve' it. Treat it as sealed.\n"
    "3. Never change the colour gradient — same colours, same direction, same\n"
    "   stops.\n"
    "4. Keep ALL text character-for-character identical; never add new text,\n"
    "   labels, tags, stamps or watermarks anywhere.\n"
    "5. The photo/subject stays unchanged except where the requested change\n"
    "   explicitly asks.\n"
    "If the requested change conflicts with a guardrail, apply only the\n"
    "permissible part and leave the protected element untouched."
)


def build_tweak_prompt(instruction: str) -> str:
    return "\n\n".join([
        "You are doing the final retouch pass on a FINISHED, approved brand "
        "creative. The attached image is that final. Apply ONE minor change and "
        "nothing else.",
        "REQUESTED CHANGE (the only intended difference):\n" + instruction.strip()[:500],
        TWEAK_GUARDRAILS,
        "Return the retouched image at the same aspect ratio.",
    ])


def apply_tweak(*, final_png: bytes, instruction: str, provider,
                width: int, height: int,
                aspect_ratio: str | None = None, image_size: str | None = None) -> dict:
    """One guardrailed retouch. Returns
    ``{"ok","png","qa","violations","prompt"}``; ``png`` is None unless ``ok``.
    Never raises."""
    base_prompt = build_tweak_prompt(instruction)
    result = {"ok": False, "png": None, "qa": "not_run",
              "violations": [], "prompt": base_prompt}
    violations: list[str] = []
    for _ in range(_MAX_TWEAK_ATTEMPTS):
        prompt = base_prompt if not violations else (
            base_prompt
            + "\n\nYOUR PREVIOUS ATTEMPT VIOLATED THESE GUARDRAILS — fix them:\n- "
            + "\n- ".join(violations)
        )
        try:
            png, _mime = provider.generate(
                prompt,
                reference_images=[(final_png, "image/png")],
                width=width, height=height,
                aspect_ratio=aspect_ratio, image_size=image_size,
            )
        except Exception:  # noqa: BLE001 - surfaced as an honest failure result
            logger.warning("tweak generate failed", exc_info=True)
            return {**result, "violations": ["image model call failed"]}
        verdict = qa_brain.check_tweak(final_png, png, instruction)
        if verdict is None:
            return {**result, "ok": True, "png": png, "qa": "skipped", "prompt": prompt}
        if verdict["passed"]:
            return {**result, "ok": True, "png": png, "qa": "passed", "prompt": prompt}
        violations = verdict["violations"] or ["guardrail check failed"]
    return {**result, "qa": "failed", "violations": violations}
