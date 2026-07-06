"""Stage-3 content tokens + prompt substitution (spec §6.3) and UI validation.

The canonical default copy (headline/highlight/sub-text/CTA) doubles as the
exact-string anchors replaced in ``stage3_text_overlay.txt``. Stage 3 renders
deterministically (``text_overlay``); the substituted prompt is what the audit
panel shows. HIGHLIGHT_PHRASE appears three times in the canonical prompt: once
inside the HEADLINE and twice in the highlight/colour spec lines — we substitute
the full HEADLINE first, then the remaining HIGHLIGHT occurrences, so the two
tokens stay isolated and consistent.
"""

from __future__ import annotations

from ..tokens import Diff, Substitution, apply_token

# ── Canonical defaults (must match the bytes in the prompt files) ──────────────
DEFAULT_HEADLINE = "Hire Experienced Virtual Legal Staff For Your Firm"
DEFAULT_HIGHLIGHT = "Virtual Legal Staff"
DEFAULT_SUBTEXT_1 = "Build your team with the best legal staff in the world."
DEFAULT_SUBTEXT_2 = "Choose from pre-vetted candidates — start in under 3 days."
DEFAULT_CTA = "Book a Free Consultation"

# Stage-3 per-element style markers (literals in stage3_text_overlay.txt). Each
# of the five text elements is styled independently: every element carries a
# font + colour, and the four positionable blocks also carry a placement. The
# highlight is rendered inline inside the headline, so it has font + colour only
# (its position follows the headline). build_prompt resolves the chosen keys to
# descriptive phrases and substitutes them here; with no styles passed every
# marker stays untouched, so the canonical file is byte-identical (§9.1).
STAGE3_STYLE_ANCHORS: dict[str, dict[str, str]] = {
    "headline":  {"font": "[HEADLINE_FONT]",  "color": "[HEADLINE_COLOR]",  "placement": "[HEADLINE_PLACEMENT]"},
    "highlight": {"font": "[HIGHLIGHT_FONT]",  "color": "[HIGHLIGHT_COLOR]"},
    "subtext1":  {"font": "[SUBTEXT1_FONT]",   "color": "[SUBTEXT1_COLOR]",  "placement": "[SUBTEXT1_PLACEMENT]"},
    "subtext2":  {"font": "[SUBTEXT2_FONT]",   "color": "[SUBTEXT2_COLOR]",  "placement": "[SUBTEXT2_PLACEMENT]"},
    "cta":       {"font": "[CTA_FONT]",        "placement": "[CTA_PLACEMENT]"},
}

def substitute_stage3(
    template: str,
    *,
    headline: str | None = None,
    highlight: str | None = None,
    subtext1: str | None = None,
    subtext2: str | None = None,
    cta: str | None = None,
    styles: dict | None = None,
) -> Substitution:
    """Apply the §6.3 content tokens and the per-element style markers to the
    Stage-3 prompt.

    ``styles`` maps each element key (headline/highlight/subtext1/subtext2/cta)
    to a dict of already-resolved descriptive values:
    ``{"font": "Causten Bold", "color": "<phrase>", "placement": "<phrase>"}``.
    Only the keys present for that element in ``STAGE3_STYLE_ANCHORS`` are
    substituted. With ``styles=None`` (and no content) the prompt is returned
    byte-identical, so the canonical file's integrity is preserved (§9.1)."""
    diffs: list[Diff] = []
    warnings: list[str] = []
    t = template
    # Full headline first (unique), then the remaining standalone highlight refs.
    t = apply_token(t, "HEADLINE", DEFAULT_HEADLINE, headline, diffs, warnings)
    t = apply_token(t, "SUBTEXT_LINE_1", DEFAULT_SUBTEXT_1, subtext1, diffs, warnings)
    t = apply_token(t, "SUBTEXT_LINE_2", DEFAULT_SUBTEXT_2, subtext2, diffs, warnings)
    t = apply_token(t, "CTA_TEXT", DEFAULT_CTA, cta, diffs, warnings)
    t = apply_token(t, "HIGHLIGHT_PHRASE", DEFAULT_HIGHLIGHT, highlight, diffs, warnings)

    styles = styles or {}
    for element, anchors in STAGE3_STYLE_ANCHORS.items():
        s = styles.get(element) or {}
        for attr, anchor in anchors.items():
            t = apply_token(t, f"{element}.{attr}", anchor, s.get(attr), diffs, warnings)
    return Substitution(text=t, diffs=diffs, warnings=warnings)


# ── UI-side validation for content tokens (§6.3) ──────────────────────────────
def validate_stage3_tokens(
    *, headline: str, highlight: str, subtext1: str, subtext2: str, cta: str
) -> list[str]:
    """Return a list of validation errors (empty = valid)."""
    errors: list[str] = []
    if len(headline.split()) > 9:
        errors.append("Headline must be ≤ 9 words.")
    if highlight not in headline:
        errors.append("Highlight phrase must be a contiguous substring of the headline.")
    if len(subtext1) > 70:
        errors.append("Sub-text line 1 must be ≤ 70 characters.")
    if len(subtext2) > 70:
        errors.append("Sub-text line 2 must be ≤ 70 characters.")
    if len(cta.split()) > 4:
        errors.append("CTA must be ≤ 4 words.")
    return errors
