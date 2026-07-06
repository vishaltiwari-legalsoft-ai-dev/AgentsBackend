"""Stage-1 prompt substitution (spec §6) — the aspect-ratio token swap.

Uses the shared exact-string substitution engine in ``..tokens``; when the user
picked the canonical 16:9 the swap is a no-op, so the prompt stays byte-identical
to the integrity-locked .txt file (§9.1).
"""

from __future__ import annotations

from ..tokens import ASPECT_RATIOS, DEFAULT_AR, Diff, Substitution, apply_token

# Stage-1 AR anchor. Every canonical Stage-1 prompt opens with the literal
# "16:9 aspect ratio" (the bytes are frozen — see prompts.CANONICAL_SHA256), so
# the studio used to render every background at 16:9 regardless of the user's
# selection. We substitute this anchor for the selected AR at build time, exactly
# like the Stage-2/3 tokens, so the prompt text agrees with the API's image_config
# while the .txt files stay byte-identical (§9.1).
STAGE1_AR_ANCHOR = "16:9 aspect ratio"


def substitute_stage1(template: str, aspect_ratio: str = DEFAULT_AR) -> Substitution:
    """Swap the Stage-1 prompt's hard-coded "16:9 aspect ratio" for the selected
    AR. A no-op when the user actually picked 16:9 (so 16:9 stays byte-identical
    to the canonical prompt)."""
    diffs: list[Diff] = []
    warnings: list[str] = []
    if ASPECT_RATIOS.get(aspect_ratio) is None:
        warnings.append(f"unknown aspect ratio '{aspect_ratio}' — using {DEFAULT_AR}")
        aspect_ratio = DEFAULT_AR
    t = apply_token(template, "ASPECT_RATIO", STAGE1_AR_ANCHOR,
                    f"{aspect_ratio} aspect ratio", diffs, warnings)
    return Substitution(text=t, diffs=diffs, warnings=warnings)
