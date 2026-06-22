"""Token substitution — the ONLY permitted edits to canonical prompts (spec §6).

Implemented as ordered *exact-string* replacements of the canonical DEFAULT
value with the user/agent-approved value. When a value equals its default the
replacement is a no-op, which is what guarantees the §9.1 byte-identical
property. Because we only ever replace whitelisted default strings, no other
byte can change — that is the §9.2 isolation property, by construction.

Stage 3's HIGHLIGHT_PHRASE ("Virtual Legal Staff") appears three times: once
inside the HEADLINE and twice in the highlight/colour spec lines (the spec's
"occurs twice" note counts only the latter two). We substitute the full
HEADLINE first, then the remaining HIGHLIGHT occurrences, so the two tokens stay
isolated and consistent.
"""

from __future__ import annotations

from dataclasses import dataclass, field


# ── Canonical defaults (must match the bytes in the prompt files) ──────────────
DEFAULT_FONT = "Causten Bold"
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

# Stage-2 AR anchor strings (legacy per-variant prompts; the current common
# blend prompt carries none, so AR is enforced via the API dimensions only).
_AR_DIMENSIONS_ANCHOR = "1080x1350px (4:5 aspect ratio)"
_AR_FLAG_ANCHOR = "--ar 4:5"
_AR_ORIENTATION_ANCHOR = "Vertical social media post,"

# Stage-2 subject token in the common blend prompt (stage2_element_blend.txt).
SUBJECT_ANCHOR = "[SUBJECT]"

# Stage-1 AR anchor. Every canonical Stage-1 prompt opens with the literal
# "16:9 aspect ratio" (the bytes are frozen — see prompts.CANONICAL_SHA256), so
# the studio used to render every background at 16:9 regardless of the user's
# selection. We substitute this anchor for the selected AR at build time, exactly
# like the Stage-2/3 tokens, so the prompt text agrees with the API's image_config
# while the .txt files stay byte-identical (§9.1).
STAGE1_AR_ANCHOR = "16:9 aspect ratio"


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


def _apply(text: str, token: str, default: str, value: str | None,
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


def substitute_stage1(template: str, aspect_ratio: str = DEFAULT_AR) -> Substitution:
    """Swap the Stage-1 prompt's hard-coded "16:9 aspect ratio" for the selected
    AR. A no-op when the user actually picked 16:9 (so 16:9 stays byte-identical
    to the canonical prompt)."""
    diffs: list[Diff] = []
    warnings: list[str] = []
    if ASPECT_RATIOS.get(aspect_ratio) is None:
        warnings.append(f"unknown aspect ratio '{aspect_ratio}' — using {DEFAULT_AR}")
        aspect_ratio = DEFAULT_AR
    t = _apply(template, "ASPECT_RATIO", STAGE1_AR_ANCHOR,
               f"{aspect_ratio} aspect ratio", diffs, warnings)
    return Substitution(text=t, diffs=diffs, warnings=warnings)


def substitute_stage2(
    template: str,
    variant: str | None = None,
    aspect_ratio: str = DEFAULT_AR,
    *,
    subject: str | None = None,
) -> Substitution:
    """Build a Stage-2 prompt: substitute the ``[SUBJECT]`` token, then apply any
    §6.2 aspect-ratio tokens.

    The current common blend prompt carries no AR text tokens — the aspect ratio
    is enforced purely via the API call's dimensions — so once the subject is
    substituted the template is returned otherwise untouched. We detect the
    AR case by the presence of the dimensions anchor rather than hard-coding ids.
    """
    diffs: list[Diff] = []
    warnings: list[str] = []
    template = _apply(template, "SUBJECT", SUBJECT_ANCHOR, subject, diffs, warnings)

    ar = ASPECT_RATIOS.get(aspect_ratio)
    if ar is None:
        warnings.append(f"unknown aspect ratio '{aspect_ratio}' — using {DEFAULT_AR}")
        ar = ASPECT_RATIOS[DEFAULT_AR]
        aspect_ratio = DEFAULT_AR

    if _AR_DIMENSIONS_ANCHOR not in template:
        if aspect_ratio != DEFAULT_AR:
            warnings.append("This prompt has no AR tokens — pass dimensions via the API only.")
        return Substitution(text=template, diffs=diffs, warnings=warnings)

    if aspect_ratio == DEFAULT_AR:
        return Substitution(text=template, diffs=diffs, warnings=warnings)

    t = template
    t = _apply(t, "ASPECT_RATIO.dimensions", _AR_DIMENSIONS_ANCHOR,
               f"{ar['w']}x{ar['h']}px ({aspect_ratio} aspect ratio)", diffs, warnings)
    t = _apply(t, "ASPECT_RATIO.flag", _AR_FLAG_ANCHOR, f"--ar {aspect_ratio}", diffs, warnings)
    t = _apply(t, "ASPECT_RATIO.orientation", _AR_ORIENTATION_ANCHOR,
               f"{ar['orientation']} social media post,", diffs, warnings)
    return Substitution(text=t, diffs=diffs, warnings=warnings)


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
    t = _apply(t, "HEADLINE", DEFAULT_HEADLINE, headline, diffs, warnings)
    t = _apply(t, "SUBTEXT_LINE_1", DEFAULT_SUBTEXT_1, subtext1, diffs, warnings)
    t = _apply(t, "SUBTEXT_LINE_2", DEFAULT_SUBTEXT_2, subtext2, diffs, warnings)
    t = _apply(t, "CTA_TEXT", DEFAULT_CTA, cta, diffs, warnings)
    t = _apply(t, "HIGHLIGHT_PHRASE", DEFAULT_HIGHLIGHT, highlight, diffs, warnings)

    styles = styles or {}
    for element, anchors in STAGE3_STYLE_ANCHORS.items():
        s = styles.get(element) or {}
        for attr, anchor in anchors.items():
            t = _apply(t, f"{element}.{attr}", anchor, s.get(attr), diffs, warnings)
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
