"""Content Optimizer config — loads opt_config.yaml, validates, merges vertical overrides.

The YAML file is the single source of truth for every weight and threshold.
The Layer-8 feedback loop retunes the system by writing a new version of the
YAML — never by patching code — so nothing here may carry a default that
contradicts the file.
"""
from __future__ import annotations

import copy
import pathlib
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator

_CONFIG_PATH = pathlib.Path(__file__).with_name("opt_config.yaml")


class TermsCfg(BaseModel):
    prevalence_exponent: float = Field(ge=1.0, le=3.0)
    rank_weight_offset: float = Field(gt=0)
    universal_idf_floor: float = Field(ge=0)
    ngram_max: int = Field(ge=1, le=4)
    nested_ngram_overlap: float = Field(gt=0, le=1)
    count_range_percentiles: tuple[float, float]
    high_dispersion_cqd: float
    short_doc_words: int
    range_widen_factor: float = Field(ge=1)


class StructureCfg(BaseModel):
    percentiles: tuple[float, float]
    rank_weighted: bool
    small_n: int
    small_n_widen_factor: float = Field(ge=1)
    bimodal_min_separation: float
    bimodal_min_fraction: float = Field(gt=0, lt=0.5)
    features: list[str]


class ScoreWeights(BaseModel):
    term: float
    semantic: float
    structure: float

    @field_validator("structure")
    @classmethod
    def _sums_to_one(cls, v: float, info) -> float:
        total = v + info.data.get("term", 0) + info.data.get("semantic", 0)
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"score weights must sum to 1, got {total}")
        return v


class ScoreCfg(BaseModel):
    weights: ScoreWeights
    band_decay_factor: float = Field(gt=0)
    semantic_floor_credit: float = Field(ge=0, le=1)
    coverage_complete: float = Field(gt=0, le=1)
    shortness_floor: float = Field(ge=0, le=1)
    stuffing_p90_multiplier: float = Field(ge=1)
    stuffing_penalty_points: float = Field(ge=0)


class SerpCfg(BaseModel):
    top_n: int = Field(ge=10, le=50)
    fetch_concurrency: int = Field(ge=1, le=16)
    per_domain_delay_s: float = Field(ge=0)
    fetch_timeout_s: float = Field(gt=0)
    fetch_retries: int = Field(ge=0, le=5)
    article_share_min: float = Field(gt=0, le=1)
    volatility_overlap_low: float = Field(ge=0, le=1)
    forum_domains: list[str]
    video_domains: list[str]
    product_domains: list[str]
    product_path_tokens: list[str]


class ExtractCfg(BaseModel):
    link_density_max: float = Field(gt=0, le=1)
    min_block_chars: int
    thin_doc_words: int
    max_doc_words: int
    interstitial_max_share: float = Field(gt=0, le=1)
    min_content_image_px: int
    shingle_size: int = Field(ge=2)
    duplicate_jaccard: float = Field(gt=0, le=1)
    comment_class_tokens: list[str]
    interstitial_phrases: list[str]


class SemanticCfg(BaseModel):
    similarity_threshold: float = Field(gt=0, lt=1)
    embed_model: str
    gemini_embed_model: str
    chunk_max_sentences: int = Field(ge=1)
    chunk_overlap_sentences: int = Field(ge=0)
    cluster_merge_threshold: float = Field(gt=0, lt=1)
    min_cluster_docs: int = Field(ge=1)
    hubness_max_matches: int = Field(ge=1)
    reciprocal_top_k: int = Field(ge=1)


class OptimizerConfig(BaseModel):
    version: int
    terms: TermsCfg
    structure: StructureCfg
    score: ScoreCfg
    serp: SerpCfg
    extract: ExtractCfg
    semantic: SemanticCfg


def _deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(vertical: str | None = None, path: pathlib.Path | None = None) -> OptimizerConfig:
    raw: dict[str, Any] = yaml.safe_load((path or _CONFIG_PATH).read_text(encoding="utf-8"))
    verticals = raw.pop("verticals", {}) or {}
    if vertical:
        if vertical not in verticals:
            raise KeyError(f"unknown vertical profile: {vertical!r}")
        raw = _deep_merge(raw, verticals[vertical])
    return OptimizerConfig.model_validate(raw)
