"""Campaign Performance Reporting (requirements §3.1).

Aggregates spend + funnel counts per channel/UTM, derives cost metrics, applies
the 2026 thresholds, and computes week-over-week deltas.
"""

from __future__ import annotations

from collections import defaultdict

from .. import goals
from ..schemas import CampaignMetric, Flag


def _safe_div(n: float, d: float) -> float | None:
    return round(n / d, 2) if d else None


def aggregate_by_channel(metrics: list[CampaignMetric]) -> dict[str, dict]:
    acc: dict[str, dict] = defaultdict(
        lambda: dict(spend=0.0, leads=0, qualified_leads=0, demos_booked=0, demos_completed=0)
    )
    for m in metrics:
        a = acc[m.channel]
        a["spend"] += m.spend
        a["leads"] += m.leads
        a["qualified_leads"] += m.qualified_leads
        a["demos_booked"] += m.demos_booked
        a["demos_completed"] += m.demos_completed
    for a in acc.values():
        a["cost_per_lead"] = _safe_div(a["spend"], a["leads"])
        a["cost_per_qualified_lead"] = _safe_div(a["spend"], a["qualified_leads"])
        a["cost_per_demo_booked"] = _safe_div(a["spend"], a["demos_booked"])
        a["cost_per_demo_completed"] = _safe_div(a["spend"], a["demos_completed"])
        a["cac"] = a["cost_per_demo_completed"]
    return dict(acc)


def top_utm_sources(metrics: list[CampaignMetric], limit: int = 5) -> list[dict]:
    acc: dict[str, dict] = defaultdict(lambda: dict(spend=0.0, demos_booked=0))
    for m in metrics:
        a = acc[m.utm_source]
        a["spend"] += m.spend
        a["demos_booked"] += m.demos_booked
    rows = [dict(utm_source=k, **v) for k, v in acc.items()]
    rows.sort(key=lambda r: r["demos_booked"], reverse=True)
    return rows[:limit]


def flag_all(metrics: list[CampaignMetric], prior: dict[str, float] | None = None) -> list[Flag]:
    """Run every metric through the 2026 threshold rules. ``prior`` maps a
    channel to its prior 7-day conversion rate (for the >30% drop check)."""
    prior = prior or {}
    flags: list[Flag] = []
    for m in metrics:
        flags.extend(goals.evaluate(m, prior_conversion=prior.get(m.channel)))
    return flags


def week_over_week(current: dict[str, dict], previous: dict[str, dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for ch, cur in current.items():
        prev = previous.get(ch, {})
        out[ch] = {
            "demos_booked_delta": cur["demos_booked"] - prev.get("demos_booked", 0),
            "spend_delta": round(cur["spend"] - prev.get("spend", 0.0), 2),
        }
    return out
