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
# FONT_ANCHOR is the literal string baked into the immutable Stage-3 prompt
# (spec §6.1). It is NOT a user-selectable value — it exists only as the
# substitution target. The brand font is LOCKED to the Causten family (see
# ``variants.FONT_FAMILY``), so every run substitutes this anchor for a Causten
# variant; the default selection is ``DEFAULT_FONT`` below.
FONT_ANCHOR = "Artica Bold"
DEFAULT_FONT = "Causten Bold"
DEFAULT_HEADLINE = "Hire Experienced Virtual Legal Staff For Your Firm"
DEFAULT_HIGHLIGHT = "Virtual Legal Staff"
DEFAULT_SUBTEXT_1 = "Build your team with the best legal staff in the world."
DEFAULT_SUBTEXT_2 = "Choose from pre-vetted candidates — start in under 3 days."
DEFAULT_CTA = "Book a Free Consultation"

# Stage-3 placement tokens (markers in stage3_text_overlay.txt). The UI lets the
# user choose where the text block and CTA sit; build_prompt resolves the chosen
# key to a descriptive phrase (see variants.TEXT_PLACEMENTS / CTA_PLACEMENTS) and
# substitutes it here. Defaults reproduce the original left-aligned layout.
TEXT_PLACEMENT_ANCHOR = "[TEXT_PLACEMENT]"
CTA_PLACEMENT_ANCHOR = "[CTA_PLACEMENT]"
DEFAULT_TEXT_PLACEMENT = "left"
DEFAULT_CTA_PLACEMENT = "bottom"

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
    font: str | None = None,
    text_placement: str | None = None,
    cta_placement: str | None = None,
) -> Substitution:
    """Apply the §6.1 font token, §6.3 content tokens and the placement tokens to
    the Stage-3 prompt. ``text_placement`` / ``cta_placement`` are the resolved
    descriptive phrases (not the UI keys)."""
    diffs: list[Diff] = []
    warnings: list[str] = []
    t = template
    # Font is locked to the Causten family: the immutable prompt carries the
    # FONT_ANCHOR literal, which we always replace with the selected Causten
    # variant (the default is itself a Causten variant, so this is never a no-op).
    t = _apply(t, "FONT", FONT_ANCHOR, font, diffs, warnings)
    # Full headline first (unique), then the remaining standalone highlight refs.
    t = _apply(t, "HEADLINE", DEFAULT_HEADLINE, headline, diffs, warnings)
    t = _apply(t, "SUBTEXT_LINE_1", DEFAULT_SUBTEXT_1, subtext1, diffs, warnings)
    t = _apply(t, "SUBTEXT_LINE_2", DEFAULT_SUBTEXT_2, subtext2, diffs, warnings)
    t = _apply(t, "CTA_TEXT", DEFAULT_CTA, cta, diffs, warnings)
    t = _apply(t, "HIGHLIGHT_PHRASE", DEFAULT_HIGHLIGHT, highlight, diffs, warnings)
    t = _apply(t, "TEXT_PLACEMENT", TEXT_PLACEMENT_ANCHOR, text_placement, diffs, warnings)
    t = _apply(t, "CTA_PLACEMENT", CTA_PLACEMENT_ANCHOR, cta_placement, diffs, warnings)
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
