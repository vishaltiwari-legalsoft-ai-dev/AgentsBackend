"""2026 performance goals + red-flag thresholds, encoded verbatim as data.

Source: the requirements doc "2026 Goals" section. The verbatim values are the
DEFAULTS; the team can edit any figure from the UI, which persists a JSON
overrides file (``MR_TARGETS_FILE``, default ``<agent>/targets.json``). All
threshold logic lives in ``evaluate`` and reads the effective (merged) targets.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from .schemas import CampaignMetric, Flag

_TARGETS_FILE_DEFAULT = Path(__file__).resolve().parents[1] / "targets.json"

# --- Report rules (requirements §3.1) -------------------------------------
COST_PER_BOOKING_FLAG = 150.0   # flag campaigns where cost-per-booking > $150
CONVERSION_DROP_PCT = 0.30      # flag >30% drop vs prior 7-day average

# --- Red-flag thresholds (requirements "2026 Goals" metric table) ---------
SPEND_NO_DEMO_LIMIT = 3000.0    # $3000+ spend with no demo
CPQL_RED = 600.0                # cost per qualified lead red flag
CAC_RED = 3000.0                # CAC red flag
MGMT_FEE_LIMIT = 3000.0         # management fees under $3000/month

_DEFAULT_THRESHOLDS: dict[str, float] = {
    "cost_per_booking_flag": COST_PER_BOOKING_FLAG,
    "conversion_drop_pct": CONVERSION_DROP_PCT,
    "spend_no_demo_limit": SPEND_NO_DEMO_LIMIT,
    "cost_per_qualified_lead_red": CPQL_RED,
    "cost_per_qualified_lead_target_low": 200.0,
    "cost_per_qualified_lead_target_high": 400.0,
    "cac_red": CAC_RED,
    "cac_target": 2500.0,
    "mgmt_fee_limit": MGMT_FEE_LIMIT,
}


@dataclass(frozen=True)
class ChannelGoal:
    channel: str
    cpd_booked_low: float
    cpd_booked_high: float
    cpd_completed_low: float
    cpd_completed_high: float
    completed_demo_pct: float  # e.g. 0.55 == 55%


# Verbatim from "PER CHANNEL PERFORMANCE GOAL".
CHANNEL_GOALS: dict[str, ChannelGoal] = {
    "Email": ChannelGoal("Email", 350, 400, 450, 600, 0.55),
    "META": ChannelGoal("META", 400, 550, 700, 850, 0.55),
    "Google": ChannelGoal("Google", 550, 750, 850, 1000, 0.75),
    "Websites": ChannelGoal("Websites", 60, 75, 100, 125, 0.65),
    "Total": ChannelGoal("Total", 500, 650, 850, 1000, 0.63),
}

# Collective all-brand targets (requirements "2026 ALL BRAND COLLECTIVE TOTAL").
COLLECTIVE = {
    "qualified_demos_goal": (2800, 3000),
    "completed_demos_goal": 2000,
    "cost_per_qualified_demo_booked": (500, 650),
    "cost_per_demo_completed": (850, 1000),
    "qualified_lead_ratio": 0.75,
    "cost_per_qualified_lead_target": (200, 400),
    "revenue_sold_goal": 185000,
}


# --- editable targets (overrides store) ------------------------------------

def _targets_path() -> Path:
    return Path(os.environ.get("MR_TARGETS_FILE") or _TARGETS_FILE_DEFAULT)


def _load_overrides() -> dict:
    p = _targets_path()
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


_GOAL_FIELDS = ("cpd_booked_low", "cpd_booked_high",
                "cpd_completed_low", "cpd_completed_high", "completed_demo_pct")


def get_targets() -> dict:
    """Effective targets: verbatim 2026 defaults merged with saved edits."""
    ov = _load_overrides()
    thresholds = dict(_DEFAULT_THRESHOLDS)
    for k, v in (ov.get("thresholds") or {}).items():
        if k in thresholds and isinstance(v, (int, float)):
            thresholds[k] = float(v)
    channel_goals: dict[str, dict] = {}
    goal_ov = ov.get("channel_goals") or {}
    for name, g in CHANNEL_GOALS.items():
        merged = {f: getattr(g, f) for f in _GOAL_FIELDS}
        for k, v in (goal_ov.get(name) or {}).items():
            if k in merged and isinstance(v, (int, float)):
                merged[k] = float(v)
        channel_goals[name] = merged
    return {"thresholds": thresholds, "channel_goals": channel_goals,
            "edited": bool(ov.get("thresholds") or goal_ov)}


def set_targets(update: dict) -> dict:
    """Merge an edit into the overrides file; returns the effective targets.
    Unknown keys and non-numeric values are rejected."""
    ov = _load_overrides()
    thr_in = update.get("thresholds") or {}
    goals_in = update.get("channel_goals") or {}
    for k, v in thr_in.items():
        if k not in _DEFAULT_THRESHOLDS:
            raise ValueError(f"unknown threshold '{k}'")
        if not isinstance(v, (int, float)) or v < 0:
            raise ValueError(f"threshold '{k}' must be a non-negative number")
    for name, fields in goals_in.items():
        if name not in CHANNEL_GOALS:
            raise ValueError(f"unknown channel '{name}'")
        for k, v in (fields or {}).items():
            if k not in _GOAL_FIELDS:
                raise ValueError(f"unknown goal field '{k}'")
            if not isinstance(v, (int, float)) or v < 0:
                raise ValueError(f"goal '{name}.{k}' must be a non-negative number")
    if thr_in:
        ov.setdefault("thresholds", {}).update({k: float(v) for k, v in thr_in.items()})
    for name, fields in goals_in.items():
        if fields:
            ov.setdefault("channel_goals", {}).setdefault(name, {}).update(
                {k: float(v) for k, v in fields.items()})
    _targets_path().write_text(json.dumps(ov, indent=1), encoding="utf-8")
    return get_targets()


def reset_targets() -> dict:
    p = _targets_path()
    if p.exists():
        p.unlink()
    return get_targets()


def thresholds() -> dict[str, float]:
    return get_targets()["thresholds"]


def channel_goal(channel: str) -> ChannelGoal | None:
    goals_map = get_targets()["channel_goals"]
    for key, fields in goals_map.items():
        if key.lower() == (channel or "").lower():
            return ChannelGoal(key, **fields)
    return None


def _band(value: float | None, good_max: float, warn_max: float) -> str:
    """Traffic-light status: good <= good_max < warn <= warn_max < bad."""
    if value is None:
        return "na"
    if value <= good_max:
        return "good"
    if value <= warn_max:
        return "warn"
    return "bad"


def status_for(channel: str, agg: dict) -> dict:
    """Per-cost-metric traffic-light status for an aggregated channel row, judged
    against the effective goals/thresholds. Consumed by the report UI for coloring."""
    g = channel_goal(channel)
    t = thresholds()
    status = {
        # Target $200–400; red at $600+ (defaults — all editable).
        "cost_per_qualified_lead": _band(
            agg.get("cost_per_qualified_lead"),
            t["cost_per_qualified_lead_target_high"], t["cost_per_qualified_lead_red"]),
        # Target ≤ $2,500; red at $3,000+ (defaults — editable).
        "cac": _band(agg.get("cac"), t["cac_target"], t["cac_red"]),
    }
    if g:
        status["cost_per_demo_booked"] = _band(
            agg.get("cost_per_demo_booked"), g.cpd_booked_high, g.cpd_booked_high * 1.5
        )
        status["cost_per_demo_completed"] = _band(
            agg.get("cost_per_demo_completed"), g.cpd_completed_high, g.cpd_completed_high * 1.5
        )
    return status


def goal_dict(channel: str) -> dict | None:
    g = channel_goal(channel)
    if not g:
        return None
    return {
        "cpd_booked_low": g.cpd_booked_low,
        "cpd_booked_high": g.cpd_booked_high,
        "cpd_completed_low": g.cpd_completed_low,
        "cpd_completed_high": g.cpd_completed_high,
        "completed_demo_pct": g.completed_demo_pct,
    }


def evaluate(metric: CampaignMetric, prior_conversion: float | None = None) -> list[Flag]:
    """Return every flag this metric trips against the effective thresholds."""
    flags: list[Flag] = []
    t = thresholds()

    if metric.spend >= t["spend_no_demo_limit"] and metric.demos_booked == 0:
        flags.append(Flag("red", f"${metric.spend:.0f} spend with no demo booked", "spend_no_demo"))

    cpb = metric.cost_per_demo_booked
    if cpb is not None and cpb > t["cost_per_booking_flag"]:
        flags.append(Flag("warn", f"Cost per booking ${cpb:.0f} exceeds ${t['cost_per_booking_flag']:.0f}", "cost_per_booking"))

    cpql = metric.cost_per_qualified_lead
    if cpql is not None and cpql >= t["cost_per_qualified_lead_red"]:
        flags.append(Flag("red", f"Cost per qualified lead ${cpql:.0f} at/above ${t['cost_per_qualified_lead_red']:.0f}", "cost_per_qualified_lead"))

    cac = metric.cac
    if cac is not None and cac >= t["cac_red"]:
        flags.append(Flag("red", f"CAC ${cac:.0f} at/above ${t['cac_red']:.0f}", "cac"))

    if prior_conversion:
        current = (metric.demos_booked / metric.leads) if metric.leads else 0.0
        if current < prior_conversion * (1 - t["conversion_drop_pct"]):
            drop = (prior_conversion - current) / prior_conversion
            flags.append(Flag("warn", f"Conversion dropped {drop * 100:.0f}% vs prior 7-day average", "conversion_drop"))

    goal = channel_goal(metric.channel)
    if goal and cpb is not None and cpb > goal.cpd_booked_high:
        flags.append(Flag("warn", f"{metric.channel} cost/demo booked ${cpb:.0f} over goal ${goal.cpd_booked_high:.0f}", "channel_goal"))

    return flags
