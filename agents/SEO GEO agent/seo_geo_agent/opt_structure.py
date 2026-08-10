"""Layer 5 — structural profiling with robust statistics. Pure math, no I/O.

The governing rule is absolute: order statistics (median, P25-P75) only —
never mean/stddev. Web features are heavy-tailed; one 12,000-word outlier
must not move a single target. Bimodal features report their modes instead
of a meaningless range; tiny corpora widen and say so.
"""
from __future__ import annotations

from pydantic import BaseModel

from .opt_config import StructureCfg
from .opt_terms import percentile


class StructureBand(BaseModel):
    feature: str
    n: int
    kind: str = "range"            # "range" | "modes"
    lo: float | None = None
    hi: float | None = None
    median: float | None = None
    modes: list[float] | None = None      # medians of each mode when bimodal
    mode_shares: list[float] | None = None
    confidence: str = "high"       # "high" | "medium" | "low"
    note: str = ""


def _robust_scale(values: list[float]) -> float:
    """IQR-based scale estimate (order-stat pure); ~sigma for normal data."""
    if len(values) < 2:
        return 0.0
    return (percentile(values, 75) - percentile(values, 25)) / 1.349


def bimodal_split(values: list[float], cfg: StructureCfg) -> tuple[list[float], list[float]] | None:
    """Detect a two-mode split: cut at the largest internal gap where both
    sides hold >= bimodal_min_fraction of docs, then require the mode medians
    to be separated by >= bimodal_min_separation robust scales.

    Returns (mode_medians, mode_shares) or None. n<=50 always here, so the
    O(n) gap scan is exact enough; a dip test at this n has no power anyway.
    """
    xs = sorted(values)
    n = len(xs)
    min_side = max(2, int(n * cfg.bimodal_min_fraction))
    best = None
    for i in range(min_side, n - min_side + 1):
        gap = xs[i] - xs[i - 1]
        if best is None or gap > best[0]:
            best = (gap, i)
    if best is None or best[0] <= 0:
        return None
    _, cut = best
    left, right = xs[:cut], xs[cut:]
    m1, m2 = percentile(left, 50), percentile(right, 50)
    scale = max(_robust_scale(left), _robust_scale(right), 1e-9)
    if (m2 - m1) / scale < cfg.bimodal_min_separation:
        return None
    return ([m1, m2], [len(left) / n, len(right) / n])


def band(feature: str, values: list[float], cfg: StructureCfg) -> StructureBand:
    """Target band for one structural feature across usable winners."""
    if feature not in cfg.features:
        raise ValueError(f"feature {feature!r} is not whitelisted for profiling")
    n = len(values)
    if n == 0:
        return StructureBand(feature=feature, n=0, confidence="low", note="no usable winners")

    med = percentile(values, 50)
    if n >= cfg.small_n:
        split = bimodal_split(values, cfg)
        if split:
            modes, shares = split
            return StructureBand(
                feature=feature, n=n, kind="modes", median=med,
                modes=modes, mode_shares=shares, confidence="medium",
                note="winners split into two groups — pick the mode that matches your format",
            )

    p_lo, p_hi = cfg.percentiles
    lo, hi = percentile(values, p_lo), percentile(values, p_hi)
    confidence, note = "high", ""
    if n < cfg.small_n:
        half = (hi - lo) / 2
        mid = (hi + lo) / 2
        lo = max(0.0, mid - half * cfg.small_n_widen_factor)
        hi = mid + half * cfg.small_n_widen_factor
        confidence = "low"
        note = f"only {n} usable winners — range widened, treat as a hint not a target"
    return StructureBand(
        feature=feature, n=n, lo=lo, hi=hi, median=med,
        confidence=confidence, note=note,
    )


def profile(
    feature_values: dict[str, list[float]], cfg: StructureCfg
) -> dict[str, StructureBand]:
    """Band for every whitelisted feature present in the corpus measurements."""
    return {
        f: band(f, vals, cfg)
        for f, vals in feature_values.items()
        if f in cfg.features
    }
