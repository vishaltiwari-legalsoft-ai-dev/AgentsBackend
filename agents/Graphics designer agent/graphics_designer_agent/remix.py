"""Per-generation prompt remix (Stage-1/2 presets) — variety without dishonesty.

When a run opts in (``config.remix_enabled``) and the user generates a PRESET
variant (never "AI"/"UPLOAD"), the preset's prompt text is rewritten by the
fast LLM for THIS attempt only: the user's brief is the #1 hard rule, the
brand rules stay locked (validated with the same validators the AI-suggest
paths use, retried once with the errors echoed back), and a rotating
VARIATION AXIS forces each attempt to differ.

Honesty (non-negotiable, mirrors suggest_gradient): a successful rewrite is
labeled ``{"ai": True}``; ANY failure appends the axis's deterministic
modifier clause instead and is labeled ``{"ai": False, "fallback_reason"}`` —
a fallback is never presented as AI output.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

from .suggestions import (
    _get_llm,
    _stable_seed,
    _validate_element_subject,
    _validate_gradient_prompt,
)

logger = logging.getLogger("graphics_designer.remix")

# (key, LLM art direction, deterministic fallback clause). Fallback clauses are
# texture/light/framing wording ONLY — no colours, no subjects, none of the
# Stage-2 banned words — so appending one always passes the brand validators.
AXES: list[tuple[str, str, str]] = [
    ("composition",
     "shift the composition's flow and focal placement so the layout reads differently",
     "Recompose with a noticeably different flow and focal placement."),
    ("lighting",
     "change where the light enters and how hard or soft it falls",
     "Light the design from a different direction with a softer, more diffuse falloff."),
    ("texture",
     "vary the surface texture (fine grain, silk-smoothness, subtle layering) while staying premium",
     "Add a subtly different premium surface texture to the blend."),
    ("mood",
     "adjust the mood along calm-to-energetic while staying premium",
     "Give the design a calmer, more understated premium mood."),
    ("framing",
     "change the visual framing and vignetting so the edges read differently",
     "Frame the design with a gentler, wider vignette than usual."),
    ("depth",
     "vary the sense of depth and layering",
     "Deepen the sense of layered depth across the design."),
]


def axis_for(run_id: str, attempt_count: int) -> tuple[str, str, str]:
    """Deterministic axis for this run + attempt: stable across replays, and
    consecutive regenerates of the same card sweep the whole pool."""
    return AXES[(_stable_seed(run_id) + attempt_count) % len(AXES)]


@dataclass
class RemixResult:
    text: str
    meta: dict


def _brief_text(run: dict) -> str:
    brief = (run.get("config") or {}).get("creative_brief") or {}
    return " ".join(str(v) for v in brief.values() if str(v).strip()).strip()


def _remix_ask(base_prompt: str, *, stage: int, brief: str, axis_directive: str, pack) -> str:
    what = "background gradient prompt" if stage == 1 else "foreground subject description"
    brief_rule = (
        f'1. USER BRIEF (the #1 hard rule — nothing may contradict it): "{brief}". '
        "Let it drive mood and emphasis.\n"
        if brief else ""
    )
    keep_rule = (
        "Keep every brand rule from the original intact: the exact opening anchor "
        "phrase, the exact hex colours (add none, drop none), and the closing "
        "no-text rule.\n"
        if stage == 1 else
        "Describe ONLY the foreground subject — no colour codes, and never the "
        "words background/gradient/backdrop/palette (Stage 1 owns those).\n"
    )
    return (
        f"You are an art director for {pack.name}. Rewrite the {what} below so "
        "THIS generation comes out visibly different from previous ones, without "
        "breaking any brand rule.\n\n"
        f"{brief_rule}"
        f"2. VARIATION AXIS for this attempt: {axis_directive}.\n"
        f"3. {keep_rule}"
        "4. Stay within ±40% of the original's length.\n\n"
        f"ORIGINAL:\n{base_prompt}\n\n"
        'Return ONLY minified JSON: {"prompt":"the rewritten text"}'
    )


def _fallback(base_prompt: str, modifier: str) -> str:
    return f"{base_prompt.rstrip()} {modifier}"


def remix_prompt(run: dict, stage: int, base_prompt: str, *, pack) -> RemixResult:
    """Rewrite ``base_prompt`` for this attempt. Never raises: returns either
    the validated AI rewrite or the honestly-labeled deterministic fallback."""
    attempt_count = len(run["stages"][str(stage)]["attempts"])
    key, directive, modifier = axis_for(run["id"], attempt_count)
    validate = (
        (lambda t: _validate_gradient_prompt(t, pack=pack)) if stage == 1
        else _validate_element_subject
    )
    ask = _remix_ask(base_prompt, stage=stage, brief=_brief_text(run),
                     axis_directive=directive, pack=pack)
    try:
        llm = _get_llm(temperature=0.9, fast=True)
        errors: list[str] = []
        for attempt in range(2):
            q = ask if attempt == 0 else (
                ask + "\n\nYOUR PREVIOUS ANSWER WAS REJECTED: " + " ".join(errors)
                + " Fix every issue and return ONLY the corrected minified JSON."
            )
            msg = llm.invoke(q)
            content = msg.content if isinstance(msg.content, str) else str(msg.content)
            match = re.search(r"\{.*\}", content, re.S)
            if not match:
                errors = ["The reply was not the requested minified JSON object."]
                continue
            try:
                text = str(json.loads(match.group(0)).get("prompt") or "").strip()
            except Exception:  # noqa: BLE001
                errors = ["The reply was not valid JSON."]
                continue
            errors = validate(text)
            if not errors:
                return RemixResult(text, {"ai": True, "axis": key})
        logger.warning("remix rejected by validation after retry: %s", "; ".join(errors))
        reason = ("The AI remix failed brand validation twice — "
                  "applied a deterministic variation instead.")
    except Exception:  # noqa: BLE001 - LLM unavailable → deterministic fallback
        logger.warning("remix LLM unavailable; deterministic variation applied", exc_info=True)
        reason = ("The AI remix service is unavailable — "
                  "applied a deterministic variation instead.")
    return RemixResult(_fallback(base_prompt, modifier),
                       {"ai": False, "axis": key, "fallback_reason": reason})
