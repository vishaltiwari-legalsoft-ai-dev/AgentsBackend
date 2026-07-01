"""2026 performance goals + red-flag thresholds, encoded verbatim as data.

Source: the requirements doc "2026 Goals" section. Values are copied exactly —
do not round or reinterpret. All threshold logic lives in ``evaluate``.
"""

from __future__ import annotations

from dataclasses import dataclass

from .schemas import CampaignMetric, Flag

# --- Report rules (requirements §3.1) -------------------------------------
COST_PER_BOOKING_FLAG = 150.0   # flag campaigns where cost-per-booking > $150
CONVERSION_DROP_PCT = 0.30      # flag >30% drop vs prior 7-day average

# --- Red-flag thresholds (requirements "2026 Goals" metric table) ---------
SPEND_NO_DEMO_LIMIT = 3000.0    # $3000+ spend with no demo
CPQL_RED = 600.0                # cost per qualified lead red flag
CAC_RED = 3000.0                # CAC red flag
MGMT_FEE_LIMIT = 3000.0         # management fees under $3000/month


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


def channel_goal(channel: str) -> ChannelGoal | None:
    for key, goal in CHANNEL_GOALS.items():
        if key.lower() == (channel or "").lower():
            return goal
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
    against the 2026 goals/thresholds. Consumed by the report UI for coloring."""
    g = channel_goal(channel)
    status = {
        # Target $200–400; red at $600+.
        "cost_per_qualified_lead": _band(agg.get("cost_per_qualified_lead"), 400, CPQL_RED),
        # Target ≤ $2,500; red at $3,000+.
        "cac": _band(agg.get("cac"), 2500, CAC_RED),
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
    """Return every flag this metric trips against the 2026 thresholds."""
    flags: list[Flag] = []

    if metric.spend >= SPEND_NO_DEMO_LIMIT and metric.demos_booked == 0:
        flags.append(Flag("red", f"${metric.spend:.0f} spend with no demo booked", "spend_no_demo"))

    cpb = metric.cost_per_demo_booked
    if cpb is not None and cpb > COST_PER_BOOKING_FLAG:
        flags.append(Flag("warn", f"Cost per booking ${cpb:.0f} exceeds ${COST_PER_BOOKING_FLAG:.0f}", "cost_per_booking"))

    cpql = metric.cost_per_qualified_lead
    if cpql is not None and cpql >= CPQL_RED:
        flags.append(Flag("red", f"Cost per qualified lead ${cpql:.0f} at/above ${CPQL_RED:.0f}", "cost_per_qualified_lead"))

    cac = metric.cac
    if cac is not None and cac >= CAC_RED:
        flags.append(Flag("red", f"CAC ${cac:.0f} at/above ${CAC_RED:.0f}", "cac"))

    if prior_conversion:
        current = (metric.demos_booked / metric.leads) if metric.leads else 0.0
        if current < prior_conversion * (1 - CONVERSION_DROP_PCT):
            drop = (prior_conversion - current) / prior_conversion
            flags.append(Flag("warn", f"Conversion dropped {drop * 100:.0f}% vs prior 7-day average", "conversion_drop"))

    goal = channel_goal(metric.channel)
    if goal and cpb is not None and cpb > goal.cpd_booked_high:
        flags.append(Flag("warn", f"{metric.channel} cost/demo booked ${cpb:.0f} over goal ${goal.cpd_booked_high:.0f}", "channel_goal"))

    return flags
