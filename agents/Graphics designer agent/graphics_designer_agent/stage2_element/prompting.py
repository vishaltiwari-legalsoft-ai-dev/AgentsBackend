"""Stage-2 prompt building: the [SUBJECT] substitution + prompt-steered subject
placement (spec §5.2 / §5.2b).

Uses the shared exact-string substitution engine in ``..tokens`` — when nothing
is overridden the blend prompt stays byte-identical to the canonical file (§9.1).
"""

from __future__ import annotations

from ..tokens import ASPECT_RATIOS, DEFAULT_AR, Diff, Substitution, apply_token

# Stage-2 AR anchor strings (legacy per-variant prompts; the current common
# blend prompt carries none, so AR is enforced via the API dimensions only).
_AR_DIMENSIONS_ANCHOR = "1080x1350px (4:5 aspect ratio)"
_AR_FLAG_ANCHOR = "--ar 4:5"
_AR_ORIENTATION_ANCHOR = "Vertical social media post,"

# Stage-2 subject token in the common blend prompt (stage2_element_blend.txt).
SUBJECT_ANCHOR = "[SUBJECT]"

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
    template = apply_token(template, "SUBJECT", SUBJECT_ANCHOR, subject, diffs, warnings)

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
    t = apply_token(t, "ASPECT_RATIO.dimensions", _AR_DIMENSIONS_ANCHOR,
               f"{ar['w']}x{ar['h']}px ({aspect_ratio} aspect ratio)", diffs, warnings)
    t = apply_token(t, "ASPECT_RATIO.flag", _AR_FLAG_ANCHOR, f"--ar {aspect_ratio}", diffs, warnings)
    t = apply_token(t, "ASPECT_RATIO.orientation", _AR_ORIENTATION_ANCHOR,
               f"{ar['orientation']} social media post,", diffs, warnings)
    return Substitution(text=t, diffs=diffs, warnings=warnings)


# ── Stage-2 subject placement (prompt-steered, spec §5.2b) ─────────────────────
# The Stage-2 subject is baked into the AI image, so we cannot composite it like
# the logo. Instead, when the user picks one of the 9 cells we append an explicit,
# authoritative override clause to the subject text. ``auto`` (the default), None,
# "", or any unknown key is a strict NO-OP — the subject is returned untouched, so
# the existing engine is byte-identical unless the user opts in.
_PLACEMENT_CLAUSES: dict[str, str] = {
    "top-left": "Position the subject in the top-left of the frame, keeping the rest of the frame as open negative space.",
    "top-center": "Position the subject along the top of the frame, keeping the lower area as open negative space.",
    "top-right": "Position the subject in the top-right of the frame, keeping the rest of the frame as open negative space.",
    "middle-left": "Position the subject on the left side of the frame, keeping the right side as open negative space.",
    "middle-center": "Position the subject in the center of the frame, with balanced negative space around it.",
    "middle-right": "Position the subject on the right side of the frame, keeping the left side as open negative space.",
    "bottom-left": "Position the subject in the bottom-left of the frame, keeping the rest of the frame as open negative space.",
    "bottom-center": "Position the subject along the bottom of the frame, keeping the upper area as open negative space.",
    "bottom-right": "Position the subject in the bottom-right of the frame, keeping the upper-left as open negative space.",
}

# Best-effort cleanup: curated subjects carry their own framing language (e.g.
# "She occupies the lower portion of the frame"). When the user forces a cell we
# drop sentence fragments that are primarily about position so the explicit
# choice doesn't fight the baked-in text. This is a soft assist only — the
# override clause above is authoritative even if a phrase slips through.
_POSITION_CUE = (
    "frame", "negative space", "empty space", "emptiness", "breathing room",
    "lower portion", "upper portion", "lower area", "upper area", "lower-",
    "upper-", "left side", "right side", "left open", "right open",
    "corner", "centered", "lower two-thirds",
)


def _soften_position_phrases(subject: str) -> str:
    """Drop fragments of ``subject`` that are primarily about placement.

    Splits on sentence/clause boundaries (``.`` and ``;``) and removes any
    fragment containing a position cue word. Conservative by design: if every
    fragment is dropped we keep the original rather than return an empty subject.
    """
    import re

    parts = re.split(r"([.;])", subject)
    kept: list[str] = []
    buf = ""
    for tok in parts:
        if tok in (".", ";"):
            frag = buf.strip()
            if frag and not any(cue in frag.lower() for cue in _POSITION_CUE):
                kept.append(frag)
            buf = ""
        else:
            buf += tok
    tail = buf.strip()
    if tail and not any(cue in tail.lower() for cue in _POSITION_CUE):
        kept.append(tail)
    softened = ". ".join(kept).strip()
    return softened if softened else subject.strip()


def place_subject(subject: str, placement: str | None) -> str:
    """Append an explicit placement override to a Stage-2 subject prompt.

    ``placement`` is one of the 9 cell keys (see ``variants.STAGE2_PLACEMENTS``).
    ``"auto"``, ``None``, ``""`` or any unrecognized key returns ``subject``
    unchanged — generation can never fail because of this feature.
    """
    clause = _PLACEMENT_CLAUSES.get((placement or "").strip().lower())
    if clause is None:
        return subject
    softened = _soften_position_phrases(subject).rstrip().rstrip(".")
    return f"{softened}. {clause}"
