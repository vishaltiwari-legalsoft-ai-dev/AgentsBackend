"""Layer 6 — the scoring function. Pure math, no I/O.

A scoring function is an incentive system; every exploit left in it will be
found. Hence: hard cap on term credit (repetition beyond the range earns
nothing), semantic floor credit (paraphrase is not punished), graceful band
decay (no cliffs while typing), a stuffing detector that SUBTRACTS points,
and a capped shortness penalty (concise + complete is never "bloat it").

The number is a pattern-match to current winners — not a quality measure —
and every report says so.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from .opt_config import ScoreCfg
from .opt_structure import StructureBand
from .opt_terms import percentile

DISCLAIMER = (
    "This score measures how closely the draft pattern-matches today's top-ranking "
    "pages for this query. It is inferential, not causal: backlinks, brand authority "
    "and user-behavior signals are outside the model. Treat the profile as a floor, "
    "not a ceiling."
)


class TermEntry(BaseModel):
    """One term as seen by the scorer (built by the pipeline in Phase 3)."""
    term: str
    importance: float = Field(ge=0)
    draft_count: int = Field(ge=0)
    lo: int = Field(ge=0)
    hi: int = Field(ge=0)
    subtopic_covered: bool = False   # semantic layer says the meaning is present
    brand_optin: bool = False        # ORG/PRODUCT terms are opt-in, never auto-scored


class Gap(BaseModel):
    kind: str        # "subtopic" | "term" | "structure"
    priority: float  # higher = fix first
    message: str


class ScoreReport(BaseModel):
    total: int
    term_coverage: float
    semantic_coverage: float
    structure_fit: float
    stuffing_penalty: float = 0.0
    gaps: list[Gap] = []
    winners_median_score: int | None = None
    snapshot_date: str | None = None
    locale: str | None = None
    n_docs: int | None = None
    confidence: str = "high"
    disclaimer: str = DISCLAIMER


def band_score(v: float, lo: float, hi: float, decay_factor: float) -> float:
    """Full credit inside [lo, hi]; graceful linear decay outside, reaching
    zero at decay_factor x band-width beyond the edge. Exactly per spec."""
    if lo <= v <= hi:
        return 1.0
    span = hi - lo
    if span <= 0:
        span = max(abs(hi), 1.0)  # degenerate band: decay over its own magnitude
    d = (lo - v) if v < lo else (v - hi)
    return max(0.0, 1.0 - d / (decay_factor * span))


def term_credit(entry: TermEntry, cfg: ScoreCfg) -> float:
    """Credit in [0,1] for one term. Capped: counts above the range earn the
    same as the range top — the 10th repetition buys nothing."""
    if entry.draft_count >= entry.lo and entry.lo > 0:
        return 1.0
    if 0 < entry.draft_count < entry.lo:
        return entry.draft_count / entry.lo
    if entry.draft_count == 0 and entry.subtopic_covered:
        return cfg.semantic_floor_credit
    return 0.0


def term_coverage(entries: list[TermEntry], cfg: ScoreCfg) -> float:
    scored = [e for e in entries if not e.brand_optin]
    total_w = sum(e.importance for e in scored)
    if total_w <= 0:
        return 0.0
    return sum(e.importance * term_credit(e, cfg) for e in scored) / total_w


def stuffing_penalty(
    entries: list[TermEntry],
    draft_words: int,
    corpus_density_p90: dict[str, float],
    cfg: ScoreCfg,
) -> float:
    """Points to subtract: any term whose per-100-word density exceeds the
    corpus P90 x multiplier is stuffing. One flat penalty per offending run."""
    if draft_words <= 0:
        return 0.0
    for e in entries:
        p90 = corpus_density_p90.get(e.term)
        if not p90:
            continue
        density = e.draft_count / draft_words * 100
        if density > p90 * cfg.stuffing_p90_multiplier:
            return cfg.stuffing_penalty_points
    return 0.0


def structure_fit(
    bands: dict[str, StructureBand],
    draft_features: dict[str, float],
    cfg: ScoreCfg,
    semantic_coverage: float = 0.0,
) -> float:
    """Mean band score over profiled features. When semantic coverage is
    complete, the word-count shortness penalty is floored — a concise draft
    that covers everything must never be told to pad."""
    scores: list[float] = []
    for feature, b in bands.items():
        v = draft_features.get(feature)
        if v is None:
            continue
        if b.kind == "modes" and b.modes:
            s = max(
                band_score(v, m * 0.75, m * 1.25, cfg.band_decay_factor) for m in b.modes
            )
        elif b.lo is not None and b.hi is not None:
            s = band_score(v, b.lo, b.hi, cfg.band_decay_factor)
        else:
            continue
        if (
            feature == "word_count"
            and b.lo is not None
            and v < b.lo
            and semantic_coverage >= cfg.coverage_complete
        ):
            s = max(s, cfg.shortness_floor)
        scores.append(s)
    return sum(scores) / len(scores) if scores else 0.0


def blend(
    term_cov: float, semantic_cov: float, struct_fit: float, cfg: ScoreCfg,
    penalty_points: float = 0.0,
) -> int:
    w = cfg.weights
    raw = 100.0 * (w.term * term_cov + w.semantic * semantic_cov + w.structure * struct_fit)
    return int(round(max(0.0, min(100.0, raw - penalty_points))))


def gap_report(
    missing_subtopics: list[tuple[str, str]],   # (label, suggested H2)
    entries: list[TermEntry],
    bands: dict[str, StructureBand],
    draft_features: dict[str, float],
    cfg: ScoreCfg,
) -> list[Gap]:
    """Ordered, actionable gaps: missing subtopics first (they move the most),
    then missing high-consensus terms, then structural deficits."""
    gaps: list[Gap] = []
    for label, heading in missing_subtopics:
        gaps.append(Gap(
            kind="subtopic", priority=3.0,
            message=f"Add a section on '{label}' — suggested H2: \"{heading}\"",
        ))
    for e in sorted(entries, key=lambda e: -e.importance):
        if e.brand_optin or term_credit(e, cfg) >= 1.0:
            continue
        rng = f"{e.lo}-{e.hi}" if e.hi > e.lo else str(e.lo)
        if e.draft_count == 0 and e.subtopic_covered:
            msg = f"Consider using the exact phrase '{e.term}' ({rng} times) — the idea is present but not the wording winners use"
        elif e.draft_count == 0:
            msg = f"Missing high-consensus term '{e.term}' — winners use it {rng} times"
        else:
            msg = f"'{e.term}' appears {e.draft_count}x; winners typically use it {rng} times"
        gaps.append(Gap(kind="term", priority=2.0 + e.importance, message=msg))
    for feature, b in bands.items():
        v = draft_features.get(feature)
        if v is None or b.kind == "modes" or b.lo is None or b.hi is None:
            continue
        s = band_score(v, b.lo, b.hi, cfg.band_decay_factor)
        if s < 1.0:
            direction = "below" if v < b.lo else "above"
            gaps.append(Gap(
                kind="structure", priority=1.0 + (1.0 - s),
                message=f"{feature}: {v:g} is {direction} the winners' band {b.lo:g}-{b.hi:g}",
            ))
    return sorted(gaps, key=lambda g: -g.priority)


STRENGTHS_CAP = 12


def strengths_report(
    covered_subtopics: list[tuple[str, str]],   # (label, evidence chunk text)
    entries: list[TermEntry],
    bands: dict[str, StructureBand],
    draft_features: dict[str, float],
    cfg: ScoreCfg,
) -> list[dict]:
    """The mirror of ``gap_report``: what the draft already does the way the
    winners do, in the same order (subtopics, terms, structure) and the same
    voice, so a page check can set pros beside cons. Plain dicts because this
    is the shape the page-check block persists. Capped so a long draft cannot
    bury three real cons under forty pros."""
    out: list[dict] = []
    for label, evidence in covered_subtopics:
        msg = f"Covers '{label}'"
        if evidence:
            msg += f" — \"{evidence[:120]}\""
        out.append({"kind": "subtopic", "message": msg})
    for e in sorted(entries, key=lambda e: -e.importance):
        if e.brand_optin or term_credit(e, cfg) < 1.0:
            continue
        rng = f"{e.lo}-{e.hi}" if e.hi > e.lo else str(e.lo)
        if e.draft_count <= e.hi:
            msg = f"'{e.term}' used {e.draft_count}x — inside the winners' {rng} range"
        else:
            msg = f"'{e.term}' used {e.draft_count}x — covers the winners' {rng} range (extra repetitions earn nothing)"
        out.append({"kind": "term", "message": msg})
    for feature, b in bands.items():
        v = draft_features.get(feature)
        if v is None or b.kind == "modes" or b.lo is None or b.hi is None:
            continue
        if band_score(v, b.lo, b.hi, cfg.band_decay_factor) >= 1.0:
            out.append({
                "kind": "structure",
                "message": f"{feature}: {v:g} is inside the winners' band {b.lo:g}-{b.hi:g}",
            })
    return out[:STRENGTHS_CAP]


def winners_median_score(winner_totals: list[float]) -> int | None:
    """Comparability anchor: 'your 31 vs winners' median 78'."""
    if not winner_totals:
        return None
    return int(round(percentile(winner_totals, 50)))
