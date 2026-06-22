"""Brand registry — the seam that turns the Graphics Designer into a multi-brand hub.

The pipeline, renderer and suggestion layer are a single SHARED ENGINE. Everything
that varies per brand — the colour palette, the brand-kit block, the Stage-1
gradient prompts, the Stage-2 element library, the font family, the default copy
and the suggestion content — lives in a per-brand ``BrandPack``. The engine asks
``get_pack(brand_id)`` for the active brand and never hard-codes one.

Phase A note: Legal Soft's data still physically lives in ``variants.py`` /
``tokens.py`` / ``suggestions.py`` / ``prompts/`` — the legalsoft pack simply
*references* it, so introducing this seam changes no behaviour and keeps the
canonical prompts byte-identical. New brands ship their own ``brands/<id>/`` data
module; relocating Legal Soft's content into ``brands/legalsoft/`` is a later,
behaviour-neutral cleanup.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from . import suggestions as _s
from . import tokens as _t
from . import variants as _v
from .prompts import CANONICAL_SHA256, PROMPT_DIR

# The default brand when a run carries no ``brand_id`` (back-compat + Legal Soft).
DEFAULT_BRAND_ID = "legalsoft"

_AGENT_ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class BrandPack:
    """Everything brand-specific the shared engine needs, for one brand."""

    id: str
    name: str
    prompts_dir: Path
    fonts_dir: Path
    canonical_sha256: dict

    # palette / kit
    locked_colors: dict
    brand_kit_block: str
    source_note_stage1: str
    text_colors: list

    # stage variant libraries
    stage1_variants: list
    stage2_variants: list
    stage2_blend_prompt: str
    stage2_categories: list

    # fonts
    font_family: str
    font_variants: list

    # default copy tokens (must match the bytes in this brand's prompt files)
    default_font: str
    default_headline: str
    default_highlight: str
    default_subtext_1: str
    default_subtext_2: str
    default_cta: str

    # The Firestore ``brands`` doc id this brand maps to, for pulling its logo at
    # Stage 4 (Issue 4). None until a brand's kit is ingested + mapped (Phase C);
    # the engine ``id`` above is a stable pack id, not the Firestore uuid.
    firestore_brand_id: str | None = None

    # suggestion content (advisory copy, tuned per brand)
    onboarding_questions: list = field(default_factory=list)
    concept_rationale: dict = field(default_factory=dict)
    hooks: dict = field(default_factory=dict)
    ctas: list = field(default_factory=list)
    qa: dict = field(default_factory=dict)
    explore_reason: dict = field(default_factory=dict)
    explore_order: list = field(default_factory=list)
    curated_gradients: list = field(default_factory=list)
    curated_elements: list = field(default_factory=list)
    brand_gradient_hexes: set = field(default_factory=set)
    # Templated brands generate their prompts from the palette/domain instead of
    # shipping .txt files. When a filename is present here, it wins over disk.
    inline_prompts: dict | None = None

    # ── prompt access (inline generated, else per-brand prompts dir) ────────────
    def load_prompt(self, filename: str) -> str:
        if self.inline_prompts and filename in self.inline_prompts:
            return self.inline_prompts[filename]
        return (self.prompts_dir / filename).read_bytes().decode("utf-8")

    def prompt_hash(self, filename: str) -> str:
        if self.inline_prompts and filename in self.inline_prompts:
            return hashlib.sha256(self.inline_prompts[filename].encode("utf-8")).hexdigest()
        return hashlib.sha256((self.prompts_dir / filename).read_bytes()).hexdigest()

    def verify_integrity(self) -> list[str]:
        problems: list[str] = []
        for name, expected in self.canonical_sha256.items():
            path = self.prompts_dir / name
            if not path.exists():
                problems.append(f"missing prompt file: {name}")
                continue
            actual = self.prompt_hash(name)
            if actual != expected:
                problems.append(f"prompt modified: {name} ({actual[:12]}… != {expected[:12]}…)")
        return problems

    # ── variant lookups ───────────────────────────────────────────────────────
    def stage1_variant(self, variant_id: str) -> dict:
        return next(v for v in self.stage1_variants if v["id"] == variant_id.upper())

    def stage2_variant(self, variant_id: str) -> dict:
        return next(v for v in self.stage2_variants if v["id"] == variant_id.upper())

    # ── fonts ──────────────────────────────────────────────────────────────────
    def font_names(self) -> list[str]:
        return [v["name"] for v in self.font_variants]

    def font_file(self, name: str) -> str:
        table = {v["name"]: v["file"] for v in self.font_variants}
        return table.get(name, table[self.default_font])

    # ── factory config defaults (used by runs.create_run) ──────────────────────
    def default_stage3_styles(self) -> dict:
        sizes = _v.DEFAULT_TEXT_SIZE_PCT
        return {
            "headline": {"font": self.default_font, "color": "dark",
                         "size_pct": sizes["headline"],
                         "placement": _t.DEFAULT_TEXT_PLACEMENT, "offset_x": 0, "offset_y": 0},
            "highlight": {"font": self.default_font, "color": "gradient"},
            "cta": {"font": self.default_font, "size_pct": sizes["cta"],
                    "placement": _t.DEFAULT_CTA_PLACEMENT, "offset_x": 0, "offset_y": 0},
        }

    def default_subheadings(self) -> list:
        def line(text: str) -> dict:
            return {"text": text, "font": self.default_font, "color": "dark",
                    "size_pct": _v.DEFAULT_TEXT_SIZE_PCT["subheading"],
                    "placement": _t.DEFAULT_TEXT_PLACEMENT, "offset_x": 0, "offset_y": 0,
                    "approved": False}
        return [line(self.default_subtext_1), line(self.default_subtext_2)]


def _build_legalsoft() -> BrandPack:
    """Legal Soft pack — references the current in-repo data (Phase A, no moves)."""
    return BrandPack(
        id="legalsoft",
        name="Legal Soft",
        prompts_dir=PROMPT_DIR,
        fonts_dir=_AGENT_ROOT / "Causten Font Family",
        canonical_sha256=CANONICAL_SHA256,
        firestore_brand_id="9717e502d6774c57a458771d1bd7c281",
        locked_colors=_v.LOCKED_COLORS,
        brand_kit_block=_v.BRAND_KIT_BLOCK,
        source_note_stage1=_v.SOURCE_NOTE_STAGE1,
        text_colors=_v.TEXT_COLORS,
        stage1_variants=_v.STAGE1_VARIANTS,
        stage2_variants=_v.STAGE2_VARIANTS,
        stage2_blend_prompt=_v.STAGE2_BLEND_PROMPT,
        stage2_categories=_v.STAGE2_CATEGORIES,
        font_family=_v.FONT_FAMILY,
        font_variants=_v.FONT_VARIANTS,
        default_font=_t.DEFAULT_FONT,
        default_headline=_t.DEFAULT_HEADLINE,
        default_highlight=_t.DEFAULT_HIGHLIGHT,
        default_subtext_1=_t.DEFAULT_SUBTEXT_1,
        default_subtext_2=_t.DEFAULT_SUBTEXT_2,
        default_cta=_t.DEFAULT_CTA,
        onboarding_questions=_s.ONBOARDING_QUESTIONS,
        concept_rationale=_s._CONCEPT_RATIONALE,
        hooks=_s._HOOKS,
        ctas=_s._CTAS,
        qa=_s._QA,
        explore_reason=_s._EXPLORE_REASON,
        explore_order=_s._EXPLORE_ORDER,
        curated_gradients=_s._CURATED_GRADIENTS,
        curated_elements=_s._CURATED_ELEMENTS,
        brand_gradient_hexes=_s._BRAND_GRADIENT_HEXES,
    )


# Registry of available brands. New brands register their pack here.
_PACKS: dict[str, BrandPack] = {}


def _registry() -> dict[str, BrandPack]:
    if not _PACKS:
        _PACKS[DEFAULT_BRAND_ID] = _build_legalsoft()
        # Templated brands (Phase C). Lazy import avoids a cycle — templated_brands
        # imports BrandPack from this module.
        from . import templated_brands

        for pack in templated_brands.build_all():
            _PACKS[pack.id] = pack
    return _PACKS


def get_pack(brand_id: str | None = None) -> BrandPack:
    """The active brand pack. Falls back to Legal Soft for unknown/None ids so the
    pipeline always has a brand (back-compat with runs created before brands)."""
    reg = _registry()
    return reg.get((brand_id or DEFAULT_BRAND_ID), reg[DEFAULT_BRAND_ID])


def list_packs() -> list[dict]:
    """Lightweight brand list for the UI selector (id + display name)."""
    return [{"id": p.id, "name": p.name} for p in _registry().values()]
