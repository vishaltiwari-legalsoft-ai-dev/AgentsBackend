"""Report builders — the seven deliverables (requirements §4).

Each ``build`` produces a Report dict with structured data + Markdown + HTML and
persists it as a run. The structured layer comes from the feature modules; the
narrative layer comes from the analysis brain (LLM online, deterministic
offline).
"""

from __future__ import annotations

import re
from datetime import date, datetime, timezone

from . import analysis, goals, runs
from .modules import campaign_reporting as cr
from .modules import funnel_analysis as fa
from .modules import opportunity_research as orr

KINDS = [
    "daily_summary",
    "weekly_summary",
    "threshold_alert",
    "competitor_digest",
    "opportunity_report",
    "utm_attribution",
    "icp_signal",
]


def _md_to_html(md: str) -> str:
    """Minimal, dependency-free Markdown -> HTML (headers + paragraphs)."""
    out: list[str] = []
    for line in md.splitlines():
        if line.startswith("# "):
            out.append(f"<h2>{line[2:]}</h2>")
        elif line.strip():
            out.append(f"<p>{line}</p>")
    return "<div class='mr-report'>" + "".join(out) + "</div>"


def _totals(agg: dict[str, dict]) -> dict:
    """Blended KPIs across all channels for the report's hero strip."""
    spend = sum(a["spend"] for a in agg.values())
    leads = sum(a["leads"] for a in agg.values())
    qualified = sum(a["qualified_leads"] for a in agg.values())
    booked = sum(a["demos_booked"] for a in agg.values())
    completed = sum(a["demos_completed"] for a in agg.values())
    div = lambda n, d: round(n / d, 2) if d else None
    return {
        "spend": round(spend, 2),
        "leads": leads,
        "qualified_leads": qualified,
        "demos_booked": booked,
        "demos_completed": completed,
        "cost_per_demo_booked": div(spend, booked),
        "cost_per_demo_completed": div(spend, completed),
    }


def _enrich(channel: str, agg: dict) -> dict:
    agg["goal"] = goals.goal_dict(channel)
    agg["status"] = goals.status_for(channel, agg)
    return agg


# Plain-language summary per flagged metric (so the report shows "14 campaigns
# over the $600 ceiling", not 14 near-identical lines).
_FLAG_LABELS = {
    "cost_per_qualified_lead": "over the $600 cost-per-qualified-lead ceiling",
    "cac": "over the $3,000 CAC ceiling",
    "spend_no_demo": "spending $3,000+ with no demo booked",
    "cost_per_booking": "over the $150 cost-per-booking target",
    "conversion_drop": "with a 30%+ drop in conversion",
    "channel_goal": "over the channel cost-per-demo-booked goal",
}


def _money_in(text: str) -> float:
    nums = [float(n.replace(",", "")) for n in re.findall(r"\$([\d,]+)", text)]
    return max(nums) if nums else 0.0


def _flag_summary(flags: list[dict]) -> list[dict]:
    """Group raw flags by metric into one summarized line each."""
    groups: dict[tuple, dict] = {}
    for f in flags:
        key = (f.get("metric"), f["level"])
        g = groups.setdefault(key, {"metric": f.get("metric"), "level": f["level"], "count": 0, "worst": 0.0})
        g["count"] += 1
        g["worst"] = max(g["worst"], _money_in(f["message"]))
    out = []
    for (metric, level), g in groups.items():
        label = _FLAG_LABELS.get(metric, (metric or "issue").replace("_", " "))
        worst = f" (worst ${g['worst']:,.0f})" if g["worst"] else ""
        out.append({
            "metric": metric,
            "level": level,
            "count": g["count"],
            "text": f"{g['count']} campaign{'s' if g['count'] != 1 else ''} {label}{worst}",
        })
    out.sort(key=lambda x: (x["level"] != "red", -x["count"]))
    return out


def _campaign_structured(ds: dict) -> dict:
    metrics = ds.get("metrics", [])
    previous = ds.get("previous_metrics")
    current_agg = cr.aggregate_by_channel(metrics)
    # A consolidated "Total" block (from an "All" roll-up view) is the totals,
    # not a channel — pull it out so it isn't double-counted in the KPI strip.
    total_block = current_agg.pop("Total", None)
    for channel, agg in current_agg.items():
        _enrich(channel, agg)
    totals = _enrich("Total", total_block) if total_block is not None else _totals(current_agg)
    flags = [f.__dict__ for f in cr.flag_all(metrics, ds.get("prior"))]
    structured = {
        "channels": current_agg,
        "totals": totals,
        "top_utm": cr.top_utm_sources(metrics),
        "flags": flags,
        "flag_summary": _flag_summary(flags),
    }
    if previous is not None:
        structured["week_over_week"] = cr.week_over_week(
            current_agg, cr.aggregate_by_channel(previous)
        )
    return structured


def _structured(kind: str, ds: dict) -> dict:
    if kind in ("daily_summary", "weekly_summary", "threshold_alert"):
        return _campaign_structured(ds)
    if kind == "utm_attribution":
        leads = ds.get("leads", [])
        return {
            "attribution": fa.attribution(leads),
            "conversion": fa.conversion_by_channel(leads),
            "best_practice_areas": fa.best_practice_areas(leads),
            "dropoff": fa.dropoff_points(leads),
            "low_booking_channels": fa.low_booking_channels(leads),
        }
    if kind == "competitor_digest":
        return {
            "competitors": [
                {"competitor": r["competitor"], "changed": r["changed"], "summary": r["summary"]}
                for r in ds.get("competitor_results", [])
            ]
        }
    if kind in ("opportunity_report", "icp_signal"):
        opps = ds.get("opportunities", [])
        today = ds.get("today", date.today())
        return {
            "ranked": [o.__dict__ for o in orr.rank(opps)],
            "stale": [o.name for o in orr.stale_outreach(opps, today)],
            "placement_issues": [o.name for o in orr.placement_issues(opps)],
        }
    return {}


def _narration_input(kind: str, s: dict) -> dict:
    """Compact, relevant slice of the structured data for the LLM — keeps the
    read focused and cheap (no giant raw-flag list)."""
    if kind in ("daily_summary", "weekly_summary", "threshold_alert"):
        keep = ("spend", "demos_booked", "demos_completed",
                "cost_per_demo_booked", "cost_per_demo_completed",
                "cost_per_qualified_lead", "cac", "goal")
        return {
            "totals": {k: (s.get("totals") or {}).get(k) for k in ("spend", "demos_completed", "cost_per_demo_completed", "qualified_leads")},
            "channels": {ch: {k: a.get(k) for k in keep} for ch, a in (s.get("channels") or {}).items()},
            "issues": s.get("flag_summary", []),
        }
    return s


def _markdown(kind: str, structured: dict) -> str:
    title = kind.replace("_", " ").title()
    narrative = analysis.narrate(kind, _narration_input(kind, structured))
    return f"# {title}\n\n{narrative}"


def build(kind: str, dataset: dict, user_id: str) -> dict:
    """Build, persist, and return one report deliverable."""
    if kind not in KINDS:
        raise ValueError(f"unknown report kind: {kind}")
    structured = _structured(kind, dataset)
    markdown = _markdown(kind, structured)
    report = {
        "id": runs.new_run_id(),
        "kind": kind,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "user_id": user_id,
        "agent_id": "a6",
        "structured": structured,
        "markdown": markdown,
        "html": _md_to_html(markdown),
    }
    runs.save_run(report)
    return report
